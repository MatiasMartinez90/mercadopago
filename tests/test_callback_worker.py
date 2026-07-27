import json
from unittest.mock import AsyncMock

import httpx
import pytest

from mercadopago_service import callback_worker


def callback_row(attempts: int = 1) -> dict:
    return {
        "id": 42,
        "callback_url": "https://consumer.example.com/payment-events",
        "payload": {"event": "payment.approved", "status": "approved"},
        "attempts": attempts,
    }


@pytest.mark.asyncio
async def test_callback_delivery_is_signed_and_idempotent(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(204)

    delivered = AsyncMock()
    monkeypatch.setattr(callback_worker, "mark_callback_delivered", delivered)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await callback_worker.deliver_callback(
            client,
            object(),
            callback_row(),
            signing_secret="callback-signing-secret-with-at-least-32-characters",
            max_attempts=12,
        )

    request = captured["request"]
    assert request.headers["idempotency-key"] == "payment-callback-42"
    assert request.headers["x-payment-signature"]
    delivered.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_retries_then_moves_to_dead_letter(monkeypatch):
    retry = AsyncMock()
    dead = AsyncMock()
    monkeypatch.setattr(callback_worker, "reschedule_callback", retry)
    monkeypatch.setattr(callback_worker, "mark_callback_dead_letter", dead)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    ) as client:
        await callback_worker.deliver_callback(
            client,
            object(),
            callback_row(attempts=3),
            signing_secret="callback-signing-secret-with-at-least-32-characters",
            max_attempts=4,
        )
        retry.assert_awaited_once()
        dead.assert_not_awaited()

        await callback_worker.deliver_callback(
            client,
            object(),
            callback_row(attempts=4),
            signing_secret="callback-signing-secret-with-at-least-32-characters",
            max_attempts=4,
        )
        dead.assert_awaited_once()


def test_retry_delay_is_bounded():
    assert callback_worker.retry_delay(1) == 5
    assert callback_worker.retry_delay(100) == 2560


@pytest.mark.asyncio
async def test_callback_decodes_jsonb_text_returned_by_asyncpg(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(204)

    delivered = AsyncMock()
    monkeypatch.setattr(callback_worker, "mark_callback_delivered", delivered)
    row = callback_row()
    row["payload"] = json.dumps(row["payload"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await callback_worker.deliver_callback(
            client,
            object(),
            row,
            signing_secret="callback-signing-secret-with-at-least-32-characters",
            max_attempts=12,
        )

    assert captured["payload"] == {"event": "payment.approved", "status": "approved"}
    delivered.assert_awaited_once()
