from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from .config import Settings

Pool = asyncpg.Pool


async def create_pool(settings: Settings) -> Pool:
    return await asyncpg.create_pool(
        settings.database_url,
        min_size=1,
        max_size=10,
        command_timeout=10,
        statement_cache_size=0,
        max_inactive_connection_lifetime=300,
    )


@asynccontextmanager
async def transaction(pool: Pool) -> AsyncIterator[asyncpg.Connection]:
    async with pool.acquire() as connection, connection.transaction():
        yield connection
