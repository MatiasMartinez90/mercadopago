from datetime import UTC, datetime, timedelta

import pytest

from mercadopago_service import reconciliation


@pytest.mark.asyncio
async def test_reconciliation_expires_missing_provider_payment(monkeypatch):
    expired: list[str] = []

    class Provider:
        async def find_payment(self, _reference):
            return None

    async def rows(_pool):
        return [{
            "id": "intent-1",
            "provider_reference": "payment:1",
            "expires_at": datetime.now(UTC) - timedelta(minutes=1),
        }]

    async def expire(_pool, intent_id):
        expired.append(intent_id)

    monkeypatch.setattr(reconciliation, "get_settings", lambda: object())
    monkeypatch.setattr(reconciliation, "provider_from_settings", lambda _settings: Provider())
    monkeypatch.setattr(reconciliation, "list_reconcilable", rows)
    monkeypatch.setattr(reconciliation, "expire_intent", expire)
    assert await reconciliation.reconcile_once(object()) == 1
    assert expired == ["intent-1"]
