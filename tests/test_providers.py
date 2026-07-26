from datetime import UTC, datetime, timedelta

import httpx
import pytest

from mercadopago_service.providers import (
    MercadoPagoProvider,
    PaymentProviderError,
    PreferenceRequest,
    ProviderItem,
)


def preference() -> PreferenceRequest:
    return PreferenceRequest(
        external_reference="tenant:order:42",
        amount=21000,
        currency="ARS",
        payer_email="buyer@example.com",
        items=(ProviderItem("product-1", "Product", 1, 21000),),
        success_url="https://shop.example.com/success",
        pending_url="https://shop.example.com/pending",
        failure_url="https://shop.example.com/failure",
        notification_url="https://payments.example.com/webhooks/mercado-pago",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        idempotency_key="tenant-order-42",
    )


@pytest.mark.asyncio
async def test_preference_never_exposes_credentials_and_sets_idempotency():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer APP_USR-secret"
        assert request.headers["x-idempotency-key"] == "tenant-order-42"
        assert b"APP_USR-secret" not in request.content
        return httpx.Response(
            201,
            json={
                "id": "pref-42",
                "init_point": "https://www.mercadopago.com/checkout/start",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await MercadoPagoProvider(
            access_token="APP_USR-secret",
            statement_descriptor="STORE",
            client=client,
        ).create_preference(preference())
    assert result.provider_preference_id == "pref-42"
    assert result.checkout_url.startswith("https://")


@pytest.mark.asyncio
async def test_preference_rejects_amount_mismatch_before_provider_call():
    invalid = preference()
    invalid = PreferenceRequest(**{**invalid.__dict__, "amount": 22000})
    provider = MercadoPagoProvider(
        access_token="APP_USR-secret",
        statement_descriptor="STORE",
    )
    with pytest.raises(PaymentProviderError, match="preference_amount_mismatch"):
        await provider.create_preference(invalid)
