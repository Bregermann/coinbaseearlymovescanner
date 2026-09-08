from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import timedelta
from statistics import mean, median

from app.config import Settings, get_settings
from app.database.connection import session_scope
from app.database.repositories import MarketRepository, ProductRepository, RotationRepository, StatusRepository
from app.timeutils import utc_now

logger = logging.getLogger(__name__)


class RotationOutcomeTracker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.record_due_observations()
            except Exception as exc:  # noqa: BLE001
                logger.warning("rotation_tracker_failed", extra={"_error_type": type(exc).__name__})
            await asyncio.sleep(self.settings.rotation_outcome_poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    async def record_due_observations(self) -> int:
        now = utc_now()
        updated = 0
        async with session_scope(self.settings) as session:
            rotation_repo = RotationRepository(session)
            product_repo = ProductRepository(session)
            market_repo = MarketRepository(session)
            for observation, recommendation in await rotation_repo.due_observations(now):
                source_product = await product_repo.get_by_asset(recommendation.source_asset)
                destination_product = await product_repo.get_by_asset(recommendation.destination_asset)
                if source_product is None or destination_product is None:
                    continue
                source_quote = await market_repo.latest_quote(source_product.product_id)
                destination_quote = await market_repo.latest_quote(destination_product.product_id)
                if source_quote is None or destination_quote is None or not recommendation.source_price:
                    continue
                if (
                    source_quote.quote_age_seconds is None
                    or destination_quote.quote_age_seconds is None
                    or source_quote.quote_age_seconds > 120.0
                    or destination_quote.quote_age_seconds > 120.0
                ):
                    continue
                source_return = (source_quote.price - recommendation.source_price) / recommendation.source_price * 100.0
                destination_return = (
                    (destination_quote.price - recommendation.destination_price)
                    / recommendation.destination_price
                    * 100.0
                )
                observation.observed_at = now
                observation.source_price = source_quote.price
                observation.destination_price = destination_quote.price
                observation.source_return_pct = source_return
                observation.destination_return_pct = destination_return
                observation.relative_return_pct = destination_return - source_return
                observation.destination_outperformed = destination_return > source_return
                updated += 1
            await StatusRepository(session).set_status(
                "rotation_tracker",
                {"last_run_at": now.isoformat(), "updated": updated},
            )
        return updated


async def rotation_backtest_report(settings: Settings | None = None, days: int = 30) -> str:
    settings = settings or get_settings()
    since = utc_now() - timedelta(days=days)
    async with session_scope(settings) as session:
        rows = await RotationRepository(session).observations_since(since)
    grouped: dict[str, list[float]] = defaultdict(list)
    for observation, _recommendation in rows:
        if observation.relative_return_pct is not None:
            grouped[observation.horizon].append(float(observation.relative_return_pct))
    lines = [
        f"Rotation relative-performance backtest ({days}d)",
        "Metric: destination return minus source-holding return.",
    ]
    if not grouped:
        lines.append("insufficient historical sample: no completed rotation outcomes in this window")
        return "\n".join(lines)
    order = {"1h": 1, "4h": 2, "12h": 3, "24h": 4, "3d": 5, "7d": 6}
    for horizon in sorted(grouped, key=lambda value: order.get(value, 99)):
        values = grouped[horizon]
        win_rate = sum(value > 0 for value in values) / len(values) * 100.0
        lines.append(
            f"{horizon}: n={len(values)} outperformance={win_rate:.1f}% "
            f"mean_relative={mean(values):+.2f}% median_relative={median(values):+.2f}%"
        )
    return "\n".join(lines)
