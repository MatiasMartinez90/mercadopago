from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from . import __version__
from .config import Settings, get_settings
from .database import Pool, create_pool
from .models import (
    CreatePaymentIntent,
    DemoSettlement,
    PaymentPreference,
    PaymentStatusView,
)
from .providers import MercadoPagoProvider, PaymentProviderError
from .repository import (
    IdempotencyConflict,
    PaymentNotFound,
    finish_event,
    register_event,
)
from .security import (
    InvalidSignature,
    validate_api_key,
    validate_mercado_pago_signature,
)
from .service import (
    PaymentIntentNotReady,
    apply_provider_payment,
    create_payment,
    get_payment_status,
    settle_demo,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.pool = await create_pool(settings)
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(
    title="Mercado Pago integration service",
    version=__version__,
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)


def settings_for(request: Request) -> Settings:
    return request.app.state.settings


def pool_for(request: Request) -> Pool:
    return request.app.state.pool


async def require_api_key(
    settings: Annotated[Settings, Depends(settings_for)],
    x_api_key: Annotated[str, Header(alias="X-API-Key")],
) -> None:
    if not validate_api_key(x_api_key, settings.api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def readiness(
    pool: Annotated[Pool, Depends(pool_for)],
    settings: Annotated[Settings, Depends(settings_for)],
) -> dict[str, str]:
    await pool.fetchval("SELECT 1")
    return {
        "status": "ok",
        "provider": settings.provider,
        "environment": settings.environment,
    }


@app.post(
    "/v1/payment-intents",
    response_model=PaymentPreference,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
    tags=["payments"],
)
async def create_intent(
    payload: CreatePaymentIntent,
    pool: Annotated[Pool, Depends(pool_for)],
    settings: Annotated[Settings, Depends(settings_for)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=16, max_length=150),
    ],
) -> dict:
    try:
        return await create_payment(pool, settings, payload, idempotency_key)
    except IdempotencyConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PaymentIntentNotReady as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PaymentProviderError as error:
        http_status = 503 if error.retryable else 502
        raise HTTPException(status_code=http_status, detail=error.code) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get(
    "/v1/public/payment-status/{token}",
    response_model=PaymentStatusView,
    tags=["payments"],
)
async def payment_status(
    token: str,
    pool: Annotated[Pool, Depends(pool_for)],
    settings: Annotated[Settings, Depends(settings_for)],
) -> dict:
    try:
        return await get_payment_status(pool, settings, token)
    except (InvalidSignature, PaymentNotFound) as error:
        raise HTTPException(status_code=404, detail="payment not found") from error


@app.post(
    "/v1/demo/{token}",
    response_model=PaymentStatusView,
    tags=["payments"],
)
async def demo_settlement(
    token: str,
    payload: DemoSettlement,
    pool: Annotated[Pool, Depends(pool_for)],
    settings: Annotated[Settings, Depends(settings_for)],
) -> dict:
    if settings.provider != "demo" or settings.environment == "production":
        raise HTTPException(status_code=404, detail="not found")
    try:
        return await settle_demo(pool, settings, token, payload.outcome)
    except (InvalidSignature, PaymentNotFound) as error:
        raise HTTPException(status_code=404, detail="payment not found") from error


@app.post("/webhooks/mercado-pago", status_code=status.HTTP_202_ACCEPTED, tags=["webhooks"])
async def mercado_pago_webhook(
    request: Request,
    pool: Annotated[Pool, Depends(pool_for)],
    settings: Annotated[Settings, Depends(settings_for)],
) -> dict[str, str]:
    if settings.provider != "mercado_pago":
        raise HTTPException(status_code=404, detail="not found")
    payload = await request.json()
    data_id = str((payload.get("data") or {}).get("id") or request.query_params.get("data.id") or "")
    event_type = str(payload.get("type") or "unknown")
    action = str(payload.get("action") or "unknown")
    if not data_id:
        raise HTTPException(status_code=400, detail="missing payment id")
    try:
        validate_mercado_pago_signature(
            x_signature=request.headers.get("x-signature", ""),
            x_request_id=request.headers.get("x-request-id", ""),
            data_id=data_id,
            secret=settings.mercado_pago_webhook_secret,
            max_skew_seconds=settings.webhook_max_skew_seconds,
        )
    except InvalidSignature as error:
        raise HTTPException(status_code=401, detail="invalid signature") from error
    request_id = request.headers.get("x-request-id", "").strip()
    provider_event_id = request_id or f"{event_type}:{action}:{data_id}"
    event_id, created = await register_event(
        pool,
        provider="mercado_pago",
        provider_event_id=provider_event_id,
        event_type=event_type,
        payload=payload,
    )
    if not created:
        return {"status": "duplicate"}
    provider = MercadoPagoProvider(
        access_token=settings.mercado_pago_access_token,
        statement_descriptor=settings.statement_descriptor,
        api_url=settings.mercado_pago_api_url,
    )
    try:
        payment = await provider.get_payment(data_id)
        intent = await apply_provider_payment(pool, payment)
        await finish_event(pool, event_id, payment_intent_id=intent["id"])
    except (PaymentProviderError, PaymentNotFound, ValueError) as error:
        await finish_event(
            pool,
            event_id,
            payment_intent_id=None,
            error_code=getattr(error, "code", type(error).__name__),
        )
        raise HTTPException(status_code=503, detail="webhook processing failed") from error
    return {"status": "accepted"}
