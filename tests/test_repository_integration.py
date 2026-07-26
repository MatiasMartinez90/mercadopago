import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from mercadopago_service.repository import (
    IdempotencyConflict,
    apply_status,
    attach_preference,
    finish_event,
    register_event,
    reserve_intent,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    value = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    async with value.acquire() as connection:
        await connection.execute("TRUNCATE callback_outbox, payment_events, payment_intents")
    try:
        yield value
    finally:
        await value.close()


def intent_kwargs() -> dict:
    return {
        "tenant_id": "test-store",
        "consumer_reference": "order:42",
        "idempotency_key": "checkout-order-42-key",
        "request_hash": "a" * 64,
        "provider": "demo",
        "amount": 21000,
        "currency": "ARS",
        "callback_url": "https://consumer.example.com/events",
        "request_payload": {"external_reference": "order:42"},
        "expires_at": datetime.now(UTC) + timedelta(minutes=30),
    }


@pytest.mark.asyncio
async def test_intent_and_terminal_callback_are_idempotent(pool):
    intent, created = await reserve_intent(pool, **intent_kwargs())
    repeated, repeated_created = await reserve_intent(pool, **intent_kwargs())
    assert created is True
    assert repeated_created is False
    assert repeated["id"] == intent["id"]

    intent = await attach_preference(
        pool,
        intent_id=intent["id"],
        provider_preference_id="demo-pref-42",
        checkout_url="https://payments.example.com/demo/42",
        sandbox=True,
    )
    kwargs = {
        "provider_reference": intent["provider_reference"],
        "status": "approved",
        "provider_payment_id": "demo-payment-42",
        "amount": 21000,
        "currency": "ARS",
        "raw_payload": {"id": "demo-payment-42", "status": "approved"},
    }
    await apply_status(pool, **kwargs)
    await apply_status(pool, **kwargs)
    assert await pool.fetchval("SELECT count(*) FROM callback_outbox") == 1


@pytest.mark.asyncio
async def test_same_key_with_different_payload_is_rejected(pool):
    await reserve_intent(pool, **intent_kwargs())
    changed = {**intent_kwargs(), "request_hash": "b" * 64}
    with pytest.raises(IdempotencyConflict):
        await reserve_intent(pool, **changed)


@pytest.mark.asyncio
async def test_provider_amount_mismatch_never_credits_payment(pool):
    intent, _ = await reserve_intent(pool, **intent_kwargs())
    await attach_preference(
        pool,
        intent_id=intent["id"],
        provider_preference_id="demo-pref-mismatch",
        checkout_url="https://payments.example.com/demo/mismatch",
        sandbox=True,
    )
    with pytest.raises(ValueError, match="amount or currency"):
        await apply_status(
            pool,
            provider_reference=intent["provider_reference"],
            status="approved",
            provider_payment_id="fraud-payment",
            amount=1,
            currency="ARS",
            raw_payload={"id": "fraud-payment"},
        )
    assert await pool.fetchval(
        "SELECT status FROM payment_intents WHERE id = $1",
        intent["id"],
    ) == "pending"


@pytest.mark.asyncio
async def test_late_notification_does_not_regress_approved_payment(pool):
    intent, _ = await reserve_intent(pool, **intent_kwargs())
    intent = await attach_preference(
        pool,
        intent_id=intent["id"],
        provider_preference_id="demo-pref-ordering",
        checkout_url="https://payments.example.com/demo/ordering",
        sandbox=True,
    )
    base = {
        "provider_reference": intent["provider_reference"],
        "provider_payment_id": "demo-payment-ordering",
        "amount": 21000,
        "currency": "ARS",
        "raw_payload": {},
    }
    approved = await apply_status(pool, status="approved", **base)
    delayed = await apply_status(pool, status="pending", **base)
    assert approved["status"] == "approved"
    assert delayed["status"] == "approved"


@pytest.mark.asyncio
async def test_failed_provider_event_can_be_retried(pool):
    event_id, created = await register_event(
        pool,
        provider="mercado_pago",
        provider_event_id="request-42",
        event_type="payment",
        payload={"attempt": 1},
    )
    assert created is True
    await finish_event(pool, event_id, payment_intent_id=None, error_code="provider_timeout")

    repeated_id, retry = await register_event(
        pool,
        provider="mercado_pago",
        provider_event_id="request-42",
        event_type="payment",
        payload={"attempt": 2},
    )
    assert repeated_id == event_id
    assert retry is True
    assert await pool.fetchval(
        "SELECT processing_attempts FROM payment_events WHERE id = $1",
        event_id,
    ) == 2
