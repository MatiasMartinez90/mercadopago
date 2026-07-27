import asyncio
from datetime import UTC, datetime

from .config import get_settings
from .database import Pool, create_pool
from .repository import expire_intent, list_reconcilable
from .service import apply_provider_payment, provider_from_settings


async def reconcile_once(pool: Pool) -> int:
    settings = get_settings()
    provider = provider_from_settings(settings)
    rows = await list_reconcilable(pool)
    for row in rows:
        payment = await provider.find_payment(row["provider_reference"])
        if payment:
            await apply_provider_payment(pool, payment)
        elif row["expires_at"] <= datetime.now(UTC):
            await expire_intent(pool, row["id"])
    return len(rows)


async def main() -> None:
    settings = get_settings()
    pool = await create_pool(settings)
    try:
        await reconcile_once(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
