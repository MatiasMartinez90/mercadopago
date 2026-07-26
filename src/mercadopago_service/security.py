import base64
import hashlib
import hmac
import time
from dataclasses import dataclass


class InvalidSignature(ValueError):
    pass


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign_public_reference(reference: str, secret: str) -> str:
    if len(secret) < 32:
        raise ValueError("link secret must contain at least 32 characters")
    if not reference or len(reference) > 300:
        raise ValueError("invalid reference")
    encoded = _b64url(reference.encode())
    signature = _b64url(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_public_reference(token: str, secret: str) -> str:
    if len(secret) < 32 or len(token) > 1000 or token.count(".") != 1:
        raise InvalidSignature("invalid payment link")
    encoded, supplied = token.split(".", 1)
    expected = _b64url(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(supplied, expected):
        raise InvalidSignature("invalid payment link")
    try:
        reference = _unb64url(encoded).decode()
    except (ValueError, UnicodeDecodeError) as error:
        raise InvalidSignature("invalid payment link") from error
    if not reference or len(reference) > 300:
        raise InvalidSignature("invalid payment link")
    return reference


def validate_api_key(supplied: str, expected: str) -> bool:
    return bool(supplied and expected and hmac.compare_digest(supplied, expected))


@dataclass(frozen=True)
class MercadoPagoSignature:
    timestamp_ms: int
    manifest: str


def validate_mercado_pago_signature(
    *,
    x_signature: str,
    x_request_id: str,
    data_id: str,
    secret: str,
    now_ms: int | None = None,
    max_skew_seconds: int = 300,
) -> MercadoPagoSignature:
    if len(secret) < 16:
        raise InvalidSignature("webhook secret is not configured")
    parts: dict[str, list[str]] = {}
    for raw_part in x_signature.split(","):
        key, separator, value = raw_part.strip().partition("=")
        if separator and key and value:
            parts.setdefault(key, []).append(value)
    timestamp = (parts.get("ts") or [""])[0]
    signatures = parts.get("v1") or []
    try:
        timestamp_ms = int(timestamp)
    except ValueError as error:
        raise InvalidSignature("invalid webhook timestamp") from error
    current_ms = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    if abs(current_ms - timestamp_ms) > max_skew_seconds * 1000:
        raise InvalidSignature("expired webhook")
    normalized_id = data_id.lower() if data_id.isalnum() else data_id
    manifest = f"id:{normalized_id};"
    if x_request_id:
        manifest += f"request-id:{x_request_id};"
    manifest += f"ts:{timestamp};"
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    if not signatures or not any(hmac.compare_digest(value, expected) for value in signatures):
        raise InvalidSignature("invalid webhook signature")
    return MercadoPagoSignature(timestamp_ms=timestamp_ms, manifest=manifest)


def sign_callback(payload: bytes, timestamp: int, secret: str) -> str:
    if len(secret) < 32:
        raise ValueError("callback secret must contain at least 32 characters")
    manifest = str(timestamp).encode() + b"." + payload
    return hmac.new(secret.encode(), manifest, hashlib.sha256).hexdigest()
