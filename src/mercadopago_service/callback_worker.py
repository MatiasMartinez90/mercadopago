import asyncio
import json
import time

import httpx

from .config import get_settings
from .database import Pool, create_pool
from .repository import (
    claim_callbacks,
    mark_callback_delivered,
    mark_callback_dead_letter,
    reschedule_callback,
)
from .security import sign_callback


def retry_delay(attempt: int) -> int:
    return min(3600, 5 * (2 ** min(max(attempt - 1, 0), 9)))


async def deliver_callback(
    client: httpx.AsyncClient,
    pool: Pool,
    row: dict,
    *,
    signing_secret: str,
    max_attempts: int,
) -> None:
    payload = json.dumps(
        row["payload"],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    timestamp = int(time.time())
    headers = {
        "content-type": "application/json",
        "idempotency-key": f"payment-callback-{row['id']}",
        "x-payment-timestamp": str(timestamp),
        "x-payment-signature": sign_callback(payload, timestamp, signing_secret),
    }
    try:
        response = await client.post(row["callback_url"], content=payload, headers=headers)
        response.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as error:
        code = type(error).__name__
        if row["attempts"] >= max_attempts:
            await mark_callback_dead_letter(
                pool,
                row["id"],
                error_code=f"dead_letter:{code}",
            )
            return
        await reschedule_callback(
            pool,
            row["id"],
            error_code=code,
            delay_seconds=retry_delay(row["attempts"]),
        )
        return
    await mark_callback_delivered(pool, row["id"])


async def run_once(pool: Pool, client: httpx.AsyncClient) -> int:
    settings = get_settings()
    rows = await claim_callbacks(pool)
    for row in rows:
        await deliver_callback(
            client,
            pool,
            row,
            signing_secret=settings.callback_signing_secret,
            max_attempts=settings.callback_max_attempts,
        )
    return len(rows)


async def main() -> None:
    settings = get_settings()
    pool = await create_pool(settings)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5)) as client:
            while True:
                delivered = await run_once(pool, client)
                await asyncio.sleep(1 if delivered else 5)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
