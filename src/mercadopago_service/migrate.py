import asyncio
import hashlib
import os
from pathlib import Path

import asyncpg

from .config import get_settings

MIGRATIONS = Path(
    os.getenv("PAYMENTS_MIGRATIONS_DIR", str(Path.cwd() / "db" / "migrations"))
)


def migration_up(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    up, marker, _down = source.partition("-- migrate:down")
    if not marker or "-- migrate:up" not in up:
        raise ValueError(f"invalid migration markers: {path.name}")
    return up.replace("-- migrate:up", "", 1).strip()


async def migrate() -> None:
    connection = await asyncpg.connect(get_settings().database_url, command_timeout=30)
    try:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS service_migrations (
                version text PRIMARY KEY,
                checksum char(64) NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        await connection.execute("SELECT pg_advisory_lock(hashtext('mercadopago-migrations'))")
        try:
            for path in sorted(MIGRATIONS.glob("*.sql")):
                sql = migration_up(path)
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                existing = await connection.fetchval(
                    "SELECT checksum FROM service_migrations WHERE version = $1",
                    path.name,
                )
                if existing:
                    if existing != checksum:
                        raise RuntimeError(f"applied migration changed: {path.name}")
                    continue
                async with connection.transaction():
                    await connection.execute(sql)
                    await connection.execute(
                        "INSERT INTO service_migrations (version, checksum) VALUES ($1, $2)",
                        path.name,
                        checksum,
                    )
        finally:
            await connection.execute("SELECT pg_advisory_unlock(hashtext('mercadopago-migrations'))")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(migrate())
