import hashlib
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from urllib.parse import quote
from uuid import UUID

from .config import Settings
from .database import Pool
from .models import CreatePaymentIntent
from .providers import (
    DemoPaymentProvider,
    MercadoPagoProvider,
    PaymentProvider,
    PreferenceRequest,
    ProviderItem,
    ProviderPayment,
)
from .repository import (
    PaymentNotFound,
    apply_status,
    attach_preference,
    get_intent,
    mark_creation_failed,
    reserve_intent,
)
from .security import InvalidSignature, sign_public_reference, verify_public_reference


class PaymentIntentNotReady(RuntimeError):
    pass


def provider_from_settings(settings: Settings) -> PaymentProvider:
    if settings.provider == "demo":
        return DemoPaymentProvider(settings.public_url, settings.link_secret)
    return MercadoPagoProvider(
        access_token=settings.mercado_pago_access_token,
        statement_descriptor=settings.statement_descriptor,
        api_url=settings.mercado_pago_api_url,
    )


def _request_hash(request: CreatePaymentIntent) -> str:
    canonical = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def status_token(intent: dict, secret: str) -> str:
    return sign_public_reference(f"{intent['tenant_id']}:{intent['id']}", secret)


def public_view(intent: dict, secret: str) -> dict:
    return {
        "checkout_url": intent["checkout_url"],
        "status_token": status_token(intent, secret),
        "status": intent["status"],
        "amount": intent["amount"],
        "currency": intent["currency"],
        "expires_at": intent["expires_at"],
        "sandbox": intent["sandbox"],
    }


async def create_payment(
    pool: Pool,
    settings: Settings,
    request: CreatePaymentIntent,
    idempotency_key: str,
) -> dict:
    now = datetime.now(UTC)
    if request.expires_at <= now or request.expires_at > now + timedelta(hours=24):
        raise ValueError("expires_at must be in the next 24 hours")
    callback_host = (urlparse(str(request.callback_url)).hostname or "").lower()
    if callback_host not in settings.allowed_callback_hosts():
        raise ValueError("callback host is not allowed")
    if settings.environment == "production" and request.callback_url.scheme != "https":
        raise ValueError("production callbacks must use HTTPS")
    provider = provider_from_settings(settings)
    intent, created = await reserve_intent(
        pool,
        tenant_id=request.tenant_id,
        consumer_reference=request.external_reference,
        idempotency_key=idempotency_key,
        request_hash=_request_hash(request),
        provider=provider.name,
        amount=request.amount,
        currency=request.currency,
        callback_url=str(request.callback_url),
        request_payload=request.model_dump(mode="json"),
        expires_at=request.expires_at,
    )
    if not created:
        if intent["status"] == "pending" and intent["checkout_url"]:
            return public_view(intent, settings.link_secret)
        raise PaymentIntentNotReady(f"payment intent is {intent['status']}")

    preference = PreferenceRequest(
        external_reference=intent["provider_reference"],
        amount=request.amount,
        currency=request.currency,
        payer_email=str(request.payer_email) if request.payer_email else None,
        items=tuple(
            ProviderItem(
                reference=item.reference,
                title=item.title,
                quantity=item.quantity,
                unit_price=item.unit_price,
            )
            for item in request.items
        ),
        success_url=str(request.success_url),
        pending_url=str(request.pending_url),
        failure_url=str(request.failure_url),
        notification_url=f"{settings.public_url.rstrip('/')}/webhooks/mercado-pago",
        expires_at=request.expires_at,
        idempotency_key=idempotency_key,
    )
    try:
        result = await provider.create_preference(preference)
        checkout_url = result.checkout_url
        if provider.name == "demo":
            token = status_token(intent, settings.link_secret)
            checkout_url = (
                f"{settings.public_url.rstrip('/')}/demo-checkout/{quote(token, safe='')}"
            )
        intent = await attach_preference(
            pool,
            intent_id=UUID(str(intent["id"])),
            provider_preference_id=result.provider_preference_id,
            checkout_url=checkout_url,
            sandbox=result.sandbox,
        )
    except Exception:
        await mark_creation_failed(pool, UUID(str(intent["id"])))
        raise
    return public_view(intent, settings.link_secret)


def decode_status_token(token: str, secret: str) -> tuple[str, UUID]:
    reference = verify_public_reference(token, secret)
    tenant_id, separator, raw_id = reference.rpartition(":")
    if not separator or not tenant_id:
        raise InvalidSignature("invalid payment link")
    try:
        return tenant_id, UUID(raw_id)
    except ValueError as error:
        raise InvalidSignature("invalid payment link") from error


async def get_payment_status(pool: Pool, settings: Settings, token: str) -> dict:
    tenant_id, intent_id = decode_status_token(token, settings.link_secret)
    intent = await get_intent(pool, intent_id=intent_id, tenant_id=tenant_id)
    return {
        "tenant_id": intent["tenant_id"],
        "external_reference": intent["consumer_reference"],
        "status": intent["status"],
        "amount": intent["amount"],
        "currency": intent["currency"],
        "expires_at": intent["expires_at"],
        "sandbox": intent["sandbox"],
    }


async def settle_demo(
    pool: Pool,
    settings: Settings,
    token: str,
    outcome: str,
) -> dict:
    tenant_id, intent_id = decode_status_token(token, settings.link_secret)
    intent = await get_intent(pool, intent_id=intent_id, tenant_id=tenant_id)
    if intent["provider"] != "demo":
        raise PaymentNotFound("payment intent not found")
    intent = await apply_status(
        pool,
        provider_reference=intent["provider_reference"],
        status=outcome,
        provider_payment_id=f"demo-{intent_id}",
        amount=intent["amount"],
        currency=intent["currency"],
        raw_payload={"id": f"demo-{intent_id}", "status": outcome},
    )
    return await get_payment_status(pool, settings, status_token(intent, settings.link_secret))


async def apply_provider_payment(pool: Pool, payment: ProviderPayment) -> dict:
    return await apply_status(
        pool,
        provider_reference=payment.external_reference,
        status=payment.status,
        provider_payment_id=payment.provider_payment_id,
        amount=payment.amount,
        currency=payment.currency,
        raw_payload=payment.raw,
    )
