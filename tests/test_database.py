from unittest.mock import AsyncMock

import pytest

from mercadopago_service import database
from mercadopago_service.config import Settings


@pytest.mark.asyncio
async def test_pool_disables_prepared_statement_cache_for_pgbouncer(monkeypatch):
    create_pool = AsyncMock(return_value=object())
    monkeypatch.setattr(database.asyncpg, "create_pool", create_pool)

    settings = Settings(
        database_url="postgresql://payments:secret@pooler/payments",
        api_key="api-key-long-enough-for-validation",
        callback_signing_secret="callback-secret-long-enough-for-validation",
    )

    await database.create_pool(settings)

    create_pool.assert_awaited_once_with(
        settings.database_url,
        min_size=1,
        max_size=10,
        command_timeout=10,
        statement_cache_size=0,
        max_inactive_connection_lifetime=300,
    )
