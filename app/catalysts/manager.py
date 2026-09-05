from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Iterable

from app.catalysts import binance, bithumb, bybit, coinbase, kraken, okx, projects, upbit
from app.catalysts.base import CatalystScanner
from app.config import Settings, get_settings
from app.database.connection import session_scope
from app.database.repositories import CatalystRepository, StatusRepository
from app.timeutils import utc_now

logger = logging.getLogger(__name__)


class CatalystManager:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.known_assets: set[str] = set()
        self.scanners: list[CatalystScanner] = self._build_scanners()
        self._primed_sources: set[str] = set()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def set_known_assets(self, assets: Iterable[str]) -> None:
        self.known_assets = {asset.upper() for asset in assets}

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="catalyst-manager")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def poll_once(self) -> int:
        if not self.known_assets:
            return 0
        inserted = 0
        for scanner in self.scanners:
            try:
                events = await scanner.poll(self.known_assets)
            except Exception as exc:  # noqa: BLE001
                logger.warning("catalyst_scanner_failed", extra={"_source": scanner.source, "_error": str(exc)})
                continue
            if scanner.source not in self._primed_sources:
                self._primed_sources.add(scanner.source)
                logger.info("catalyst_source_primed", extra={"_source": scanner.source, "_events_seen": len(events)})
                continue
            async with session_scope(self.settings) as session:
                repo = CatalystRepository(session)
                for event in events:
                    model = await repo.insert_if_new(event.to_record())
                    if model:
                        inserted += 1
                        logger.info("catalyst_detected", extra={"_source": event.source, "_headline": event.headline, "_assets": event.affected_assets})
                await StatusRepository(session).set_status(
                    "catalyst_scanner",
                    {"last_poll_at": utc_now().isoformat(), "new_events": inserted, "sources": [s.source for s in self.scanners]},
                )
        return inserted

    async def fresh_for_asset(self, asset: str) -> list[dict[str, object]]:
        since = utc_now() - timedelta(minutes=self.settings.catalyst_fresh_window_minutes)
        async with session_scope(self.settings) as session:
            rows = await CatalystRepository(session).catalysts_for_asset(asset, since)
            return [
                {
                    "id": row.id,
                    "source": row.source,
                    "headline": row.headline,
                    "url": row.url,
                    "announcement_time": row.announcement_time,
                    "first_detected_time": row.first_detected_time,
                    "affected_assets": row.affected_assets,
                    "catalyst_type": row.catalyst_type,
                    "strength": row.strength,
                }
                for row in rows
            ]

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self.poll_once()
            await asyncio.sleep(self.settings.catalyst_poll_seconds)

    def _build_scanners(self) -> list[CatalystScanner]:
        scanners: list[CatalystScanner] = [
            coinbase.build_scanner(),
            upbit.build_scanner(),
            bithumb.build_scanner(),
            binance.build_scanner(),
            kraken.build_scanner(),
            okx.build_scanner(),
            bybit.build_scanner(),
        ]
        extra_urls = [url.strip() for url in self.settings.catalyst_source_urls.split(",") if url.strip()]
        extra = projects.build_scanner(extra_urls)
        if extra:
            scanners.append(extra)
        return scanners
