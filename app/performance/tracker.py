from __future__ import annotations

import asyncio
import logging

from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.database.connection import session_scope
from app.database.models import AlertModel, MarketTickModel
from app.database.repositories import AlertRepository, MarketRepository, StatusRepository
from app.timeutils import utc_now

logger = logging.getLogger(__name__)


class PerformanceTracker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.record_due_observations()
            except Exception as exc:  # noqa: BLE001
                logger.warning("performance_tracker_failed", extra={"_error": str(exc)})
            await asyncio.sleep(60)

    def stop(self) -> None:
        self._stop.set()

    async def record_due_observations(self) -> int:
        now = utc_now()
        updated = 0
        async with session_scope(self.settings) as session:
            alert_repo = AlertRepository(session)
            market_repo = MarketRepository(session)
            observations = await alert_repo.due_observations(now)
            for observation in observations:
                alert = await session.get(AlertModel, observation.alert_id)
                if alert is None:
                    continue
                quote = await market_repo.latest_quote(alert.product_id)
                if quote is None:
                    continue
                high, low = await price_extremes(session, alert.product_id, alert.detection_time, now)
                observation.observed_at = now
                observation.price = quote.price
                observation.return_pct = (quote.price - alert.detection_price) / alert.detection_price * 100.0
                observation.mfe_pct = ((high or quote.price) - alert.detection_price) / alert.detection_price * 100.0
                observation.mae_pct = ((low or quote.price) - alert.detection_price) / alert.detection_price * 100.0
                observation.tp1_hit = bool(alert.tp1 and high and high >= alert.tp1)
                observation.tp2_hit = bool(alert.tp2 and high and high >= alert.tp2)
                observation.stretch_hit = bool(alert.stretch and high and high >= alert.stretch)
                observation.invalidation_hit = bool(alert.invalidation_price and low and low <= alert.invalidation_price)
                observation.invalidation_before_tp1 = observation.invalidation_hit and not observation.tp1_hit
                updated += 1
            await StatusRepository(session).set_status("performance_tracker", {"last_run_at": now.isoformat(), "updated": updated})
        return updated


async def price_extremes(session, product_id: str, start, end) -> tuple[float | None, float | None]:
    result = await session.execute(
        select(func.max(MarketTickModel.price), func.min(MarketTickModel.price)).where(
            MarketTickModel.product_id == product_id,
            MarketTickModel.ingestion_time >= start,
            MarketTickModel.ingestion_time <= end,
        )
    )
    row = result.one_or_none()
    if row is None:
        return None, None
    return row[0], row[1]
