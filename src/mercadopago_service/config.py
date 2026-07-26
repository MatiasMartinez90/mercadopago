from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PAYMENTS_",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Literal["development", "test", "production"] = "development"
    provider: Literal["demo", "mercado_pago"] = "demo"
    public_url: str = "http://localhost:8080"
    database_url: str = "postgresql://payments:payments@localhost:5432/payments"
    api_key: str = Field(default="development-api-key-change-me-000000", min_length=32)
    link_secret: str = Field(default="development-link-secret-change-me-000", min_length=32)
    callback_signing_secret: str = Field(
        default="development-callback-secret-change-000",
        min_length=32,
    )
    mercado_pago_access_token: str = ""
    mercado_pago_webhook_secret: str = ""
    mercado_pago_api_url: str = "https://api.mercadopago.com"
    statement_descriptor: str = "COMMERCE"
    webhook_max_skew_seconds: int = Field(default=300, ge=30, le=900)

    @model_validator(mode="after")
    def validate_provider_credentials(self) -> "Settings":
        if self.environment == "production" and "change-me" in (
            self.api_key + self.link_secret + self.callback_signing_secret
        ):
            raise ValueError("production secrets must be explicitly configured")
        if self.provider == "mercado_pago":
            if not self.mercado_pago_access_token or not self.mercado_pago_webhook_secret:
                raise ValueError("Mercado Pago credentials are required for the real provider")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
