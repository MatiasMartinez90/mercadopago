from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import quote

import httpx

from .security import sign_public_reference


class PaymentProviderError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ProviderItem:
    reference: str
    title: str
    quantity: int
    unit_price: int


@dataclass(frozen=True)
class PreferenceRequest:
    external_reference: str
    amount: int
    currency: str
    payer_email: str | None
    items: tuple[ProviderItem, ...]
    success_url: str
    pending_url: str
    failure_url: str
    notification_url: str
    expires_at: datetime
    idempotency_key: str


@dataclass(frozen=True)
class PreferenceResult:
    provider_preference_id: str
    checkout_url: str
    sandbox: bool


@dataclass(frozen=True)
class ProviderPayment:
    provider_payment_id: str
    external_reference: str
    status: str
    amount: int
    currency: str
    raw: dict = field(repr=False)


class PaymentProvider(Protocol):
    name: str

    async def create_preference(self, request: PreferenceRequest) -> PreferenceResult: ...

    async def get_payment(self, payment_id: str) -> ProviderPayment: ...

    async def find_payment(self, external_reference: str) -> ProviderPayment | None: ...


def normalize_provider_status(value: str) -> str:
    if value == "approved":
        return "approved"
    if value in {"pending", "in_process", "in_mediation", "authorized"}:
        return "pending"
    if value == "rejected":
        return "rejected"
    if value in {"cancelled", "cancelled_by_user"}:
        return "cancelled"
    if value in {"refunded", "charged_back"}:
        return "refunded"
    raise PaymentProviderError("provider_status_unknown")


class DemoPaymentProvider:
    name = "demo"

    def __init__(self, public_url: str, link_secret: str):
        self.public_url = public_url.rstrip("/")
        self.link_secret = link_secret

    async def create_preference(self, request: PreferenceRequest) -> PreferenceResult:
        token = sign_public_reference(request.external_reference, self.link_secret)
        return PreferenceResult(
            provider_preference_id=f"demo-{request.external_reference}",
            checkout_url=f"{self.public_url}/demo-checkout/{quote(token, safe='')}",
            sandbox=True,
        )

    async def get_payment(self, payment_id: str) -> ProviderPayment:
        raise PaymentProviderError("demo_lookup_not_supported")

    async def find_payment(self, external_reference: str) -> ProviderPayment | None:
        return None


class MercadoPagoProvider:
    name = "mercado_pago"

    def __init__(
        self,
        *,
        access_token: str,
        statement_descriptor: str,
        api_url: str = "https://api.mercadopago.com",
        client: httpx.AsyncClient | None = None,
    ):
        if not access_token:
            raise ValueError("Mercado Pago access token is required")
        self.access_token = access_token
        self.statement_descriptor = statement_descriptor[:22]
        self.api_url = api_url.rstrip("/")
        self.client = client

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key[:150]
        return headers

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5))
        try:
            response = await client.request(method, f"{self.api_url}{path}", **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise PaymentProviderError("provider_unavailable", retryable=True) from error
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code >= 400:
            retryable = response.status_code in {408, 423, 429} or response.status_code >= 500
            raise PaymentProviderError(f"provider_http_{response.status_code}", retryable=retryable)
        try:
            return response.json()
        except ValueError as error:
            raise PaymentProviderError("provider_invalid_response") from error

    async def create_preference(self, request: PreferenceRequest) -> PreferenceResult:
        items = [
            {
                "id": item.reference,
                "title": item.title,
                "quantity": item.quantity,
                "currency_id": request.currency,
                "unit_price": item.unit_price,
            }
            for item in request.items
        ]
        if sum(item["quantity"] * item["unit_price"] for item in items) != request.amount:
            raise PaymentProviderError("preference_amount_mismatch")
        payload: dict = {
            "items": items,
            "back_urls": {
                "success": request.success_url,
                "pending": request.pending_url,
                "failure": request.failure_url,
            },
            "notification_url": request.notification_url,
            "auto_return": "approved",
            "external_reference": request.external_reference,
            "statement_descriptor": self.statement_descriptor,
            "expires": True,
            "expiration_date_to": request.expires_at.isoformat(),
            "metadata": {"payment_reference": request.external_reference},
        }
        if request.payer_email:
            payload["payer"] = {"email": request.payer_email}
        value = await self._request(
            "POST",
            "/checkout/preferences",
            json=payload,
            headers=self._headers(request.idempotency_key),
        )
        preference_id = str(value.get("id") or "")
        checkout_url = str(value.get("init_point") or value.get("sandbox_init_point") or "")
        if not preference_id or not checkout_url.startswith("https://"):
            raise PaymentProviderError("provider_invalid_preference")
        return PreferenceResult(
            provider_preference_id=preference_id,
            checkout_url=checkout_url,
            sandbox="sandbox" in checkout_url,
        )

    async def get_payment(self, payment_id: str) -> ProviderPayment:
        if not payment_id or len(payment_id) > 100:
            raise PaymentProviderError("provider_payment_id_invalid")
        value = await self._request(
            "GET",
            f"/v1/payments/{quote(payment_id, safe='')}",
            headers=self._headers(),
        )
        return self._normalize_payment(value)

    def _normalize_payment(self, value: dict) -> ProviderPayment:
        try:
            decimal_amount = Decimal(str(value["transaction_amount"]))
            if decimal_amount != decimal_amount.to_integral_value():
                raise ValueError("fractional amount")
            amount = int(decimal_amount)
            return ProviderPayment(
                provider_payment_id=str(value["id"]),
                external_reference=str(value["external_reference"]),
                status=normalize_provider_status(str(value["status"])),
                amount=amount,
                currency=str(value["currency_id"]),
                raw=value,
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise PaymentProviderError("provider_invalid_payment") from error

    async def find_payment(self, external_reference: str) -> ProviderPayment | None:
        if not external_reference or len(external_reference) > 300:
            raise PaymentProviderError("provider_reference_invalid")
        value = await self._request(
            "GET",
            "/v1/payments/search",
            params={
                "sort": "date_created",
                "criteria": "desc",
                "external_reference": external_reference,
            },
            headers=self._headers(),
        )
        results = value.get("results")
        if not isinstance(results, list):
            raise PaymentProviderError("provider_invalid_response")
        candidates = [row for row in results if isinstance(row, dict)]
        if not candidates:
            return None
        approved = next((row for row in candidates if row.get("status") == "approved"), None)
        return self._normalize_payment(approved or candidates[0])
