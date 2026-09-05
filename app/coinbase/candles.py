from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Iterable

from app.coinbase.rest import CoinbaseRestClient
from app.config import Settings, get_settings
from app.database.repositories import MarketRepository
from app.timeutils import utc_now

logger = logging.getLogger(__name__)


async def bootstrap_product_history(
    rest: CoinbaseRestClient,
    market_repo: MarketRepository,
    product_id: str,
    settings: Settings | None = None,
) -> int:
    settings = settings or get_settings()
    now = utc_now()
    total = 0
    ranges = [
        ("ONE_MINUTE", now - timedelta(hours=24), now),
        ("FIVE_MINUTE", now - timedelta(days=min(settings.bootstrap_history_days, 7)), now),
        ("ONE_HOUR", now - timedelta(days=settings.bootstrap_history_days), now),
    ]
    for granularity, start, end in ranges:
        async for candles in rest.iter_candle_ranges(product_id, start, end, granularity=granularity):
            total += await market_repo.upsert_candles(candles, source="coinbase-rest")
    return total


async def bootstrap_many_products(
    rest: CoinbaseRestClient,
    market_repo: MarketRepository,
    product_ids: Iterable[str],
    settings: Settings | None = None,
) -> dict[str, int]:
    settings = settings or get_settings()
    semaphore = asyncio.Semaphore(settings.bootstrap_concurrency)
    product_list = list(product_ids)[: settings.max_products_for_bootstrap_per_cycle]
    results: dict[str, int] = {}

    async def run_one(product_id: str) -> None:
        async with semaphore:
            try:
                results[product_id] = await bootstrap_product_history(rest, market_repo, product_id, settings=settings)
                logger.info("history_bootstrapped", extra={"_product_id": product_id, "_candles": results[product_id]})
            except Exception as exc:  # noqa: BLE001
                logger.warning("history_bootstrap_failed", extra={"_product_id": product_id, "_error": str(exc)})
                results[product_id] = 0

    await asyncio.gather(*(run_one(product_id) for product_id in product_list))
    return results
