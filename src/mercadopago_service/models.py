from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, HttpUrl, model_validator

PaymentStatus = Literal[
    "creating",
    "pending",
    "approved",
    "rejected",
    "cancelled",
    "refunded",
    "failed",
]


class PaymentItem(BaseModel):
    reference: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    quantity: int = Field(ge=1, le=1000)
    unit_price: int = Field(ge=1, le=1_000_000_000)


class CreatePaymentIntent(BaseModel):
    tenant_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")
    external_reference: str = Field(min_length=1, max_length=300)
    amount: int = Field(ge=1, le=1_000_000_000)
    currency: str = Field(default="ARS", pattern=r"^[A-Z]{3}$")
    description: str = Field(min_length=1, max_length=200)
    payer_email: EmailStr | None = None
    items: list[PaymentItem] = Field(min_length=1, max_length=100)
    success_url: HttpUrl
    pending_url: HttpUrl
    failure_url: HttpUrl
    callback_url: HttpUrl
    expires_at: datetime
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_total(self) -> "CreatePaymentIntent":
        calculated = sum(item.quantity * item.unit_price for item in self.items)
        if calculated != self.amount:
            raise ValueError("items total must equal amount")
        if len(self.metadata) > 20:
            raise ValueError("metadata accepts at most 20 entries")
        return self


class PaymentIntent(BaseModel):
    id: str
    tenant_id: str
    external_reference: str
    provider: Literal["demo", "mercado_pago"]
    provider_preference_id: str
    checkout_url: str
    status_token: str
    status: PaymentStatus
    amount: int
    currency: str
    sandbox: bool
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class DemoSettlement(BaseModel):
    outcome: Literal["approved", "rejected"]


class PaymentPreference(BaseModel):
    checkout_url: str
    status_token: str
    status: PaymentStatus
    amount: int
    currency: str
    expires_at: datetime
    sandbox: bool


class PaymentStatusView(BaseModel):
    tenant_id: str
    external_reference: str
    status: PaymentStatus
    amount: int
    currency: str
    expires_at: datetime
    sandbox: bool
