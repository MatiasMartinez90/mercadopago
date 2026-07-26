import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from .database import Pool, transaction


class IdempotencyConflict(ValueError):
    pass


class PaymentNotFound(LookupError):
    pass


def _record(value: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(value) if value else None


async def reserve_intent(
    pool: Pool,
    *,
    tenant_id: str,
    consumer_reference: str,
    idempotency_key: str,
    request_hash: str,
    provider: str,
    amount: int,
    currency: str,
    callback_url: str,
    request_payload: dict,
    expires_at: datetime,
) -> tuple[dict[str, Any], bool]:
    async with transaction(pool) as connection:
        existing = await connection.fetchrow(
            """
            SELECT * FROM payment_intents
            WHERE tenant_id = $1 AND idempotency_key = $2
            FOR UPDATE
            """,
            tenant_id,
            idempotency_key,
        )
        if existing:
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflict("idempotency key was already used with another payload")
            return dict(existing), False
        intent_id = await connection.fetchval("SELECT gen_random_uuid()")
        provider_reference = f"payment:{intent_id}"
        row = await connection.fetchrow(
            """
            INSERT INTO payment_intents (
                id, tenant_id, consumer_reference, provider_reference,
                idempotency_key, request_hash, provider, amount, currency,
                callback_url, request_payload, expires_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12)
            RETURNING *
            """,
            intent_id,
            tenant_id,
            consumer_reference,
            provider_reference,
            idempotency_key,
            request_hash,
            provider,
            amount,
            currency,
            callback_url,
            json.dumps(request_payload),
            expires_at,
        )
        return dict(row), True


async def attach_preference(
    pool: Pool,
    *,
    intent_id: UUID,
    provider_preference_id: str,
    checkout_url: str,
    sandbox: bool,
) -> dict[str, Any]:
    row = await pool.fetchrow(
        """
        UPDATE payment_intents
        SET provider_preference_id = $2, checkout_url = $3, sandbox = $4,
            status = 'pending', updated_at = now()
        WHERE id = $1 AND status = 'creating'
        RETURNING *
        """,
        intent_id,
        provider_preference_id,
        checkout_url,
        sandbox,
    )
    if not row:
        raise PaymentNotFound("payment intent is no longer available")
    return dict(row)


async def mark_creation_failed(pool: Pool, intent_id: UUID) -> None:
    await pool.execute(
        """
        UPDATE payment_intents
        SET status = 'failed', updated_at = now()
        WHERE id = $1 AND status = 'creating'
        """,
        intent_id,
    )


async def get_intent(pool: Pool, *, intent_id: UUID, tenant_id: str) -> dict[str, Any]:
    row = await pool.fetchrow(
        "SELECT * FROM payment_intents WHERE id = $1 AND tenant_id = $2",
        intent_id,
        tenant_id,
    )
    if not row:
        raise PaymentNotFound("payment intent not found")
    return dict(row)


async def get_intent_by_provider_reference(
    pool: Pool,
    provider_reference: str,
) -> dict[str, Any]:
    row = await pool.fetchrow(
        "SELECT * FROM payment_intents WHERE provider_reference = $1",
        provider_reference,
    )
    if not row:
        raise PaymentNotFound("payment intent not found")
    return dict(row)


async def apply_status(
    pool: Pool,
    *,
    provider_reference: str,
    status: str,
    provider_payment_id: str,
    amount: int,
    currency: str,
    raw_payload: dict,
) -> dict[str, Any]:
    async with transaction(pool) as connection:
        current = await connection.fetchrow(
            """
            SELECT * FROM payment_intents
            WHERE provider_reference = $1
            FOR UPDATE
            """,
            provider_reference,
        )
        if not current:
            raise PaymentNotFound("payment intent not found")
        if current["amount"] != amount or current["currency"] != currency:
            raise ValueError("provider amount or currency mismatch")
        # Notifications can arrive out of order. Never let a delayed pending or
        # rejected event undo money that was already approved or refunded.
        if current["status"] == "refunded":
            return dict(current)
        if current["status"] == "approved" and status != "refunded":
            return dict(current)
        if current["status"] in {"rejected", "cancelled"} and status == "pending":
            return dict(current)
        if current["status"] == status:
            return dict(current)
        row = await connection.fetchrow(
            """
            UPDATE payment_intents
            SET status = $2, provider_payment_id = $3,
                last_payment_payload = $4::jsonb, updated_at = now()
            WHERE id = $1
            RETURNING *
            """,
            current["id"],
            status,
            provider_payment_id,
            json.dumps(raw_payload),
        )
        event_type = f"payment.{status}"
        payload = {
            "event": event_type,
            "payment_intent_id": str(row["id"]),
            "tenant_id": row["tenant_id"],
            "external_reference": row["consumer_reference"],
            "status": row["status"],
            "amount": row["amount"],
            "currency": row["currency"],
            "occurred_at": row["updated_at"].isoformat(),
        }
        await connection.execute(
            """
            INSERT INTO callback_outbox (
                payment_intent_id, callback_url, event_type, payload
            )
            VALUES ($1, $2, $3, $4::jsonb)
            ON CONFLICT (payment_intent_id, event_type) DO NOTHING
            """,
            row["id"],
            row["callback_url"],
            event_type,
            json.dumps(payload),
        )
        return dict(row)


async def register_event(
    pool: Pool,
    *,
    provider: str,
    provider_event_id: str,
    event_type: str,
    payload: dict,
) -> tuple[int, bool]:
    async with transaction(pool) as connection:
        row = await connection.fetchrow(
            """
            INSERT INTO payment_events (provider, provider_event_id, event_type, payload)
            VALUES ($1, $2, $3, $4::jsonb)
            ON CONFLICT (provider, provider_event_id) DO NOTHING
            RETURNING id
            """,
            provider,
            provider_event_id,
            event_type,
            json.dumps(payload),
        )
        if row:
            return int(row["id"]), True
        existing = await connection.fetchrow(
            """
            SELECT id, processing_status, processing_started_at
            FROM payment_events
            WHERE provider = $1 AND provider_event_id = $2
            FOR UPDATE
            """,
            provider,
            provider_event_id,
        )
        retryable = existing["processing_status"] == "failed" or (
            existing["processing_status"] == "received"
            and existing["processing_started_at"]
            < await connection.fetchval("SELECT now() - interval '2 minutes'")
        )
        if not retryable:
            return int(existing["id"]), False
        await connection.execute(
            """
            UPDATE payment_events
            SET processing_status = 'received',
                processing_attempts = processing_attempts + 1,
                processing_started_at = now(),
                processed_at = NULL,
                error_code = NULL,
                payload = $2::jsonb
            WHERE id = $1
            """,
            existing["id"],
            json.dumps(payload),
        )
        return int(existing["id"]), True


async def finish_event(
    pool: Pool,
    event_id: int,
    *,
    payment_intent_id: UUID | None,
    error_code: str | None = None,
) -> None:
    await pool.execute(
        """
        UPDATE payment_events
        SET payment_intent_id = $2,
            processing_status = $3,
            error_code = $4,
            processed_at = now()
        WHERE id = $1
        """,
        event_id,
        payment_intent_id,
        "failed" if error_code else "processed",
        error_code,
    )


async def claim_callbacks(pool: Pool, limit: int = 25) -> list[dict[str, Any]]:
    async with transaction(pool) as connection:
        rows = await connection.fetch(
            """
            SELECT * FROM callback_outbox
            WHERE delivered_at IS NULL
              AND dead_letter_at IS NULL
              AND available_at <= now()
            ORDER BY available_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT $1
            """,
            limit,
        )
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        claimed = await connection.fetch(
            """
            UPDATE callback_outbox
            SET attempts = attempts + 1,
                available_at = now() + interval '60 seconds'
            WHERE id = ANY($1::bigint[])
            RETURNING *
            """,
            ids,
        )
        by_id = {row["id"]: dict(row) for row in claimed}
        return [by_id[row["id"]] for row in rows]


async def mark_callback_delivered(pool: Pool, callback_id: int) -> None:
    await pool.execute(
        """
        UPDATE callback_outbox
        SET delivered_at = now(), last_error = NULL
        WHERE id = $1
        """,
        callback_id,
    )


async def reschedule_callback(
    pool: Pool,
    callback_id: int,
    *,
    error_code: str,
    delay_seconds: int,
) -> None:
    await pool.execute(
        """
        UPDATE callback_outbox
        SET last_error = $2,
            available_at = now() + make_interval(secs => $3)
        WHERE id = $1
        """,
        callback_id,
        error_code[:200],
        delay_seconds,
    )


async def mark_callback_dead_letter(
    pool: Pool,
    callback_id: int,
    *,
    error_code: str,
) -> None:
    await pool.execute(
        """
        UPDATE callback_outbox
        SET dead_letter_at = now(), last_error = $2
        WHERE id = $1
        """,
        callback_id,
        error_code[:200],
    )
