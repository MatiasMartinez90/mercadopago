import hashlib
import hmac

import pytest

from mercadopago_service.security import (
    InvalidSignature,
    sign_callback,
    sign_public_reference,
    validate_mercado_pago_signature,
    verify_public_reference,
)

SECRET = "test-secret-that-is-longer-than-thirty-two-characters"


def test_public_link_is_tamper_evident():
    token = sign_public_reference("tenant:order:42", SECRET)
    assert verify_public_reference(token, SECRET) == "tenant:order:42"
    with pytest.raises(InvalidSignature):
        verify_public_reference(token + "x", SECRET)


def test_callback_signature_covers_timestamp_and_raw_body():
    payload = b'{"status":"approved"}'
    assert sign_callback(payload, 123, SECRET) != sign_callback(payload, 124, SECRET)


def test_mercado_pago_signature_validates_manifest_and_skew():
    timestamp = 1_900_000_000_000
    manifest = f"id:42;request-id:req-1;ts:{timestamp};"
    signature = hmac.new(SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    result = validate_mercado_pago_signature(
        x_signature=f"ts={timestamp},v1={signature}",
        x_request_id="req-1",
        data_id="42",
        secret=SECRET,
        now_ms=timestamp,
    )
    assert result.manifest == manifest

    with pytest.raises(InvalidSignature, match="expired"):
        validate_mercado_pago_signature(
            x_signature=f"ts={timestamp},v1={signature}",
            x_request_id="req-1",
            data_id="42",
            secret=SECRET,
            now_ms=timestamp + 301_000,
        )
