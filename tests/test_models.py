from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from mercadopago_service.models import CreatePaymentIntent


def valid_payload() -> dict:
    return {
        "tenant_id": "demo_store",
        "external_reference": "order:42",
        "amount": 21000,
        "currency": "ARS",
        "description": "Order 42",
        "items": [
            {
                "reference": "product-1",
                "title": "Product",
                "quantity": 1,
                "unit_price": 21000,
            }
        ],
        "success_url": "https://shop.example.com/payment/success",
        "pending_url": "https://shop.example.com/payment/pending",
        "failure_url": "https://shop.example.com/payment/failure",
        "callback_url": "https://api.example.com/payment-events",
        "expires_at": datetime.now(UTC) + timedelta(minutes=30),
    }


def test_intent_requires_exact_item_total():
    payload = valid_payload()
    payload["amount"] = 22000
    with pytest.raises(ValidationError, match="items total must equal amount"):
        CreatePaymentIntent.model_validate(payload)


def test_intent_accepts_opaque_external_reference():
    intent = CreatePaymentIntent.model_validate(valid_payload())
    assert intent.external_reference == "order:42"
