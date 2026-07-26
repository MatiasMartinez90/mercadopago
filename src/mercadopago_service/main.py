from fastapi import FastAPI

from . import __version__
from .config import get_settings

app = FastAPI(
    title="Mercado Pago integration service",
    version=__version__,
    docs_url="/docs",
    redoc_url=None,
)


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def readiness() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "provider": settings.provider,
        "environment": settings.environment,
    }
