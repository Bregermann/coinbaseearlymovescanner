from __future__ import annotations

import asyncio
import logging
import os
from collections import Counter
from datetime import timedelta
from typing import Any

from app.alerts.deduplication import AlertDeduplicator
from app.alerts.discord import DiscordWebhookClient
from app.alerts.formatter import candidate_to_alert_record, candidate_to_record
from app.analysis.liquidity import metrics_from_summary
from app.analysis.microcaps import dormancy_score, market_cap_metrics_from_metadata, microcap_tags
from app.analysis.rotation import microcap_rotation_score
from app.analysis.scoring import score_candidate
from app.analysis.structure import analyze_structure
from app.analysis.targets import generate_targets
from app.analysis.volume import calculate_volume_metrics
from app.catalysts.manager import CatalystManager
from app.coinbase.products import canonicalize_spot_products
from app.coinbase.rest import CoinbaseRestClient
from app.coinbase.websocket import CoinbaseWebSocketClient
from app.config import Settings, get_settings
from app.diagnostics import (
    build_candidate_diagnostic,
    diagnostics_status,
    qualification_blockers,
    quote_age_seconds_at,
)
from app.database.connection import init_db, session_scope
from app.database.repositories import (
    AlertRepository,
    CatalystRepository,
    MarketRepository,
    MetadataRepository,
    ProductRepository,
    StatusRepository,
)
from app.metadata import CoinGeckoMetadataClient
from app.performance.tracker import PerformanceTracker
from app.timeutils import utc_now
from app.types import Candidate, CandlePoint, LiveQuote, MarketCapMetrics, ProductInfo, SetupStatus

logger = logging.getLogger(__name__)


class ScannerService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.products: dict[str, ProductInfo] = {}
        self.asset_to_product_id: dict[str, str] = {}
        self.latest_quotes: dict[str, LiveQuote] = {}
        self.latest_book_summaries: dict[str, dict[str, Any]] = {}
        self.ws_status: dict[str, Any] = {}
        self.quote_queue: asyncio.Queue[LiveQuote] = asyncio.Queue(maxsize=20_000)
        self.candle_queue: asyncio.Queue[CandlePoint] = asyncio.Queue(maxsize=20_000)
        self.book_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=20_000)
        self.ws_client: CoinbaseWebSocketClient | None = None
        self.catalysts = CatalystManager(self.settings)
        self.discord = DiscordWebhookClient(self.settings)
        self.deduplicator = AlertDeduplicator(self.settings)
        self.performance = PerformanceTracker(self.settings)
        self.started_at = utc_now()
        self.last_quote_at = None
        self.last_quote_product_id = None
        self.last_candle_at = None
        self.last_candle_product_id = None
        self.last_book_at = None
        self.last_book_product_id = None
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.started_at = utc_now()
        await init_db(self.settings)
        await self.refresh_products()
        await self.refresh_metadata()
        await self._start_websocket()
        self.catalysts.set_known_assets(self.asset_to_product_id.keys())
        await self.catalysts.start()
        async with session_scope(self.settings) as session:
            await StatusRepository(session).set_status(
                "runtime",
                {
                    "started_at": self.started_at.isoformat(),
                    "pid": os.getpid(),
                    "products": len(self.products),
                },
            )
        self._tasks = [
            asyncio.create_task(self._persistence_loop(), name="persistence-loop"),
            asyncio.create_task(self._scan_loop(), name="scan-loop"),
            asyncio.create_task(self._product_refresh_loop(), name="product-refresh-loop"),
            asyncio.create_task(self._metadata_loop(), name="metadata-loop"),
            asyncio.create_task(self._history_bootstrap_loop(), name="history-bootstrap-loop"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat-loop"),
            asyncio.create_task(self.performance.run_forever(), name="performance-tracker"),
        ]
        logger.info("scanner_started", extra={"_products": len(self.products)})

    async def stop(self) -> None:
        self._stop.set()
        await self.catalysts.stop()
        if self.ws_client:
            await self.ws_client.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.discord.__aexit__(None, None, None)

    async def run_forever(self) -> None:
        await self.start()
        try:
            while not self._stop.is_set():
                await asyncio.sleep(1)
        finally:
            await self.stop()

    async def refresh_products(self) -> None:
        async with CoinbaseRestClient(self.settings) as rest:
            raw = await rest.list_products()
        products = canonicalize_spot_products(raw)
        self.products = {product.product_id: product for product in products}
        self.asset_to_product_id = {product.base_currency: product.product_id for product in products}
        async with session_scope(self.settings) as session:
            await ProductRepository(session).upsert_products(products)
            await StatusRepository(session).set_status(
                "products",
                {"count": len(products), "assets": sorted(self.asset_to_product_id), "last_refresh_at": utc_now().isoformat()},
            )
        logger.info("products_refreshed", extra={"_count": len(products)})

    async def refresh_metadata(self) -> None:
        if not self.asset_to_product_id:
            return
        async with CoinGeckoMetadataClient(self.settings) as client:
            metadata = await client.fetch_assets(self.asset_to_product_id.keys())
        async with session_scope(self.settings) as session:
            repo = MetadataRepository(session)
            for asset, (metrics, raw) in metadata.items():
                await repo.upsert_metadata(asset, metrics, raw=raw)
            await StatusRepository(session).set_status(
                "metadata",
                {"assets_refreshed": len(metadata), "last_refresh_at": utc_now().isoformat()},
            )
        logger.info("metadata_refreshed", extra={"_count": len(metadata)})

    async def scan_once(self, persist_and_alert: bool = True) -> list[Candidate]:
        now = utc_now()
        async with session_scope(self.settings) as session:
            diagnostics, skipped = await self.evaluate_candidates(session, now, include_stale=False)
            candidates = [item.candidate for item in diagnostics if item.qualifies]
            candidates.sort(key=lambda item: item.score.early_move_score, reverse=True)
            candidates = candidates[: self.settings.candidate_limit]
            if persist_and_alert:
                alert_repo = AlertRepository(session)
                for candidate in candidates:
                    await alert_repo.insert_candidate_snapshot(candidate_to_record(candidate))
                sent = 0
                for candidate in candidates:
                    if await self._maybe_alert(session, candidate, quote_age_seconds=diagnostic_quote_age(diagnostics, candidate.product_id)):
                        sent += 1
                scan_status = {
                    "last_scan_at": now.isoformat(),
                    "qualifying": len(candidates),
                    "alerts_sent_this_scan": sent,
                    "top": [
                        {"symbol": c.symbol, "score": c.score.early_move_score, "status": c.status.value}
                        for c in candidates
                    ],
                }
                diag_status = diagnostics_status(diagnostics, skipped, products_checked=len(self.products))
                diag_status["last_scan_at"] = now.isoformat()
                diag_status["alerts_sent_this_scan"] = sent
                await StatusRepository(session).set_status("scan", scan_status)
                await StatusRepository(session).set_status("diagnostics", diag_status)
        return candidates

    async def evaluate_candidates(
        self,
        session: Any,
        now: Any | None = None,
        *,
        include_stale: bool = False,
    ) -> tuple[list[Any], dict[str, int]]:
        now = now or utc_now()
        since = now - timedelta(days=max(self.settings.analysis_history_days, 31))
        diagnostics: list[Any] = []
        skipped: Counter[str] = Counter()
        volume_by_asset = {}
        cap_by_asset: dict[str, MarketCapMetrics] = {}
        pending: list[tuple[ProductInfo, LiveQuote, float | None, list[CandlePoint], Any, dict[str, Any] | None]] = []

        market_repo = MarketRepository(session)
        metadata_repo = MetadataRepository(session)
        catalyst_repo = CatalystRepository(session)
        alert_repo = AlertRepository(session)
        metadata = await metadata_repo.all_metadata()
        fresh_catalysts = await catalyst_repo.fresh_catalysts(now - timedelta(minutes=self.settings.catalyst_fresh_window_minutes))
        catalyst_by_asset = _catalyst_map(fresh_catalysts)
        active_alerts = await alert_repo.active_alerts_by_product()

        for product in self.products.values():
            quote = self.latest_quotes.get(product.product_id) or await market_repo.latest_quote(product.product_id)
            if quote is None or quote.price <= 0:
                skipped["missing data"] += 1
                continue
            quote_age = quote_age_seconds_at(quote, now)
            if not include_stale and (
                quote_age is None
                or quote_age > self.settings.alert_max_quote_age_seconds * 3
            ):
                skipped["stale data"] += 1
                continue
            candles = await market_repo.recent_candles(product.product_id, since)
            if len(candles) < 12:
                skipped["insufficient history"] += 1
                continue
            volume = calculate_volume_metrics(candles, now)
            cap = market_cap_metrics_from_metadata(metadata.get(product.base_currency), quote)
            volume_by_asset[product.base_currency] = volume
            cap_by_asset[product.base_currency] = cap
            pending.append((product, quote, quote_age, candles, volume, catalyst_by_asset.get(product.base_currency)))

        rotation_scores = microcap_rotation_score(cap_by_asset, volume_by_asset, threshold=self.settings.microcap_threshold_usd)
        for product, quote, quote_age, candles, volume, catalyst in pending:
            structure = analyze_structure(candles, quote.price, now)
            structure.highs.update(
                await market_repo.highs_since_windows(
                    product.product_id,
                    {
                        "90d": now - timedelta(days=90),
                        "180d": now - timedelta(days=180),
                        "1y": now - timedelta(days=365),
                    },
                )
            )
            book = self.latest_book_summaries.get(product.product_id)
            if book is None:
                row = await market_repo.latest_order_book_summary(product.product_id)
                book = row.raw_summary if row and row.raw_summary else None
            liquidity = metrics_from_summary(book)
            cap = cap_by_asset[product.base_currency]
            dormant = dormancy_score(candles, now)
            rotation = rotation_scores.get(product.base_currency, 0.0)
            score = score_candidate(
                volume,
                structure,
                liquidity,
                cap,
                quote_age,
                catalyst=catalyst,
                rotation_score=rotation,
                microcap_threshold=self.settings.microcap_threshold_usd,
            )
            if dormant >= 70.0 and cap.circulating_market_cap and cap.circulating_market_cap <= self.settings.microcap_threshold_usd:
                score.components["DormancyScore"] = max(score.components.get("DormancyScore", 0.0), dormant)
            status = choose_status(structure.status, score.catalyst_score, structure.extension_24h_pct)
            targets = generate_targets(quote.price, structure)
            tags = build_tags(product, cap, volume, structure, score, self.settings.microcap_threshold_usd, catalyst)
            candidate = Candidate(
                product_id=product.product_id,
                symbol=product.base_currency,
                status=status,
                quote=quote,
                volume=volume,
                structure=structure,
                liquidity=liquidity,
                market_cap=cap,
                targets=targets,
                score=score,
                catalyst=catalyst,
                tags=tags,
            )
            active = active_alerts.get(product.product_id)
            decision = None
            if not qualification_blockers(candidate, self.settings, quote_age):
                decision = self.deduplicator.decide(candidate, active, quote_age_seconds=quote_age)
            diagnostics.append(
                build_candidate_diagnostic(
                    candidate,
                    self.settings,
                    candle_count=len(candles),
                    active_alert=active,
                    alert_decision=decision,
                    quote_age_seconds=quote_age,
                )
            )

        diagnostics.sort(key=lambda item: item.candidate.score.early_move_score, reverse=True)
        return diagnostics, dict(skipped)

    async def _maybe_alert(self, session: Any, candidate: Candidate, quote_age_seconds: float | None = None) -> bool:
        alert_repo = AlertRepository(session)
        active = await alert_repo.active_alert_for_product(candidate.product_id)
        decision = self.deduplicator.decide(candidate, active, quote_age_seconds=quote_age_seconds)
        if not decision.should_send:
            return False
        message_id = await self.discord.send_candidate(candidate, alert_type=decision.alert_type, quote_age_seconds=quote_age_seconds)
        if message_id is None:
            logger.error(
                "alert_delivery_failed_no_dedupe",
                extra={"_product_id": candidate.product_id, "_type": decision.alert_type},
            )
            return False
        catalyst_id = candidate.catalyst.get("id") if candidate.catalyst else None
        if active is not None:
            await alert_repo.close_alert(active, decision.reason)
        alert_data = candidate_to_alert_record(candidate, decision.alert_type, catalyst_id=catalyst_id)
        alert_data["discord_message_id"] = message_id
        if decision.alert_type == "invalidation":
            alert_data["is_active"] = False
            alert_data["closed_reason"] = "invalidation"
        alert = await alert_repo.create_alert(alert_data)
        if decision.alert_type != "invalidation":
            await alert_repo.create_observation_schedule(alert)
        logger.info("alert_sent", extra={"_product_id": candidate.product_id, "_type": decision.alert_type, "_score": candidate.score.early_move_score})
        return True

    async def _start_websocket(self) -> None:
        product_ids = list(self.products)
        if not product_ids:
            return
        if self.ws_client:
            await self.ws_client.stop()
        self.ws_client = CoinbaseWebSocketClient(
            product_ids,
            settings=self.settings,
            on_quote=self._on_quote,
            on_candle=self._on_candle,
            on_book_summary=self._on_book_summary,
            on_status=self._on_status,
        )
        await self.ws_client.start()

    async def _on_quote(self, quote: LiveQuote) -> None:
        self.latest_quotes[quote.product_id] = quote
        self.last_quote_at = utc_now()
        self.last_quote_product_id = quote.product_id
        await _put_latest(self.quote_queue, quote)

    async def _on_candle(self, candle: CandlePoint) -> None:
        self.last_candle_at = utc_now()
        self.last_candle_product_id = candle.product_id
        await _put_latest(self.candle_queue, candle)

    async def _on_book_summary(self, product_id: str, summary: dict[str, Any]) -> None:
        self.latest_book_summaries[product_id] = summary
        self.last_book_at = utc_now()
        self.last_book_product_id = product_id
        await _put_latest(self.book_queue, (product_id, summary))

    async def _on_status(self, key: str, value: dict[str, Any]) -> None:
        self.ws_status[key] = {"updated_at": utc_now().isoformat(), "value": summarize_ws_status(value)}

    async def _persistence_loop(self) -> None:
        while not self._stop.is_set():
            try:
                quotes = drain_queue(self.quote_queue, 1000)
                candles = drain_queue(self.candle_queue, 1000)
                books = drain_queue(self.book_queue, 250)
                if quotes or candles or books:
                    async with session_scope(self.settings) as session:
                        repo = MarketRepository(session)
                        for quote in quotes:
                            await repo.insert_tick(quote)
                        await repo.upsert_candles(candles, source="coinbase-ws")
                        for product_id, summary in books:
                            await repo.insert_order_book_summary(product_id, summary)
                    async with session_scope(self.settings) as session:
                        now = utc_now()
                        ws_client = self.ws_client
                        await StatusRepository(session).set_status(
                            "websocket",
                            {
                                "latest_quotes": len(self.latest_quotes),
                                "latest_books": len(self.latest_book_summaries),
                                "assets_subscribed": len(self.products),
                                "assets_receiving_live_data": count_recent_quotes(self.latest_quotes, 60.0),
                                "assets_fresh_for_alerts": count_recent_quotes(
                                    self.latest_quotes,
                                    self.settings.alert_max_quote_age_seconds,
                                ),
                                "connected_chunks": ws_client.connected_chunks if ws_client else 0,
                                "last_coinbase_message_at": isoformat(ws_client.last_message_at if ws_client else None),
                                "last_ticker": {
                                    "product_id": self.last_quote_product_id,
                                    "received_at": isoformat(self.last_quote_at),
                                },
                                "last_trade": {
                                    "product_id": ws_client.last_trade_product_id if ws_client else None,
                                    "received_at": isoformat(ws_client.last_trade_at if ws_client else None),
                                },
                                "last_candle": {
                                    "product_id": self.last_candle_product_id,
                                    "received_at": isoformat(self.last_candle_at),
                                },
                                "last_order_book": {
                                    "product_id": self.last_book_product_id,
                                    "received_at": isoformat(self.last_book_at),
                                },
                                "ws_status": self.ws_status,
                                "last_persist_at": now.isoformat(),
                            },
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("persistence_loop_failed", extra={"_error_type": type(exc).__name__})
            await asyncio.sleep(1)

    async def _scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.scan_once(persist_and_alert=True)
            except Exception as exc:  # noqa: BLE001
                logger.exception("scan_loop_failed", extra={"_error": str(exc)})
            await asyncio.sleep(self.settings.scan_interval_seconds)

    async def _product_refresh_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.settings.coinbase_product_refresh_seconds)
            old = set(self.products)
            try:
                await self.refresh_products()
                self.catalysts.set_known_assets(self.asset_to_product_id.keys())
                if set(self.products) != old:
                    await self._start_websocket()
            except Exception as exc:  # noqa: BLE001
                logger.warning("product_refresh_failed", extra={"_error": str(exc)})

    async def _metadata_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.settings.microcap_refresh_seconds)
            try:
                await self.refresh_metadata()
            except Exception as exc:  # noqa: BLE001
                logger.warning("metadata_refresh_failed", extra={"_error": str(exc)})

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._emit_heartbeat()
            except Exception as exc:  # noqa: BLE001
                logger.warning("heartbeat_loop_failed", extra={"_error_type": type(exc).__name__})
            await asyncio.sleep(self.settings.discord_heartbeat_seconds)

    async def _emit_heartbeat(self) -> None:
        async with session_scope(self.settings) as session:
            statuses = await StatusRepository(session).all_status()
            alert_repo = AlertRepository(session)
            now = utc_now()
            alerts_1h = len(await alert_repo.alerts_since(now - timedelta(hours=1)))
        diagnostics = statuses.get("diagnostics", {}).get("value", {})
        websocket = statuses.get("websocket", {}).get("value", {})
        score_counts = diagnostics.get("score_counts", {}) if isinstance(diagnostics, dict) else {}
        top = diagnostics.get("top_candidates", []) if isinstance(diagnostics, dict) else []
        near_miss = top[0] if top else None
        payload = {
            "username": "Coinbase Early Move Scanner",
            "embeds": [
                {
                    "title": "SCANNER ALIVE",
                    "description": "Diagnostic heartbeat. This is not a production alert.",
                    "color": 0x3498DB,
                    "fields": [
                        {"name": "Coinbase", "value": "CONNECTED" if websocket.get("connected_chunks", 0) else "UNKNOWN", "inline": True},
                        {"name": "Quote age", "value": heartbeat_quote_age(self.latest_quotes), "inline": True},
                        {"name": "Assets monitored", "value": str(len(self.products)), "inline": True},
                        {"name": "Candidates evaluated", "value": str(diagnostics.get("candidates_evaluated", 0)), "inline": True},
                        {"name": "Current >=70", "value": str(score_counts.get("gte_70", 0)), "inline": True},
                        {"name": "Production alerts 1h", "value": str(alerts_1h), "inline": True},
                        {"name": "Top near-miss", "value": format_near_miss(near_miss), "inline": False},
                    ],
                }
            ],
        }
        webhook = self.settings.debug_webhook
        if not webhook and self.settings.discord_heartbeat_allow_primary:
            webhook = self.settings.alert_webhook
        if webhook:
            await self.discord.send_payload(payload, webhook_url=webhook)
        else:
            logger.info(
                "scanner_alive",
                extra={
                    "_assets": len(self.products),
                    "_fresh_assets": count_recent_quotes(self.latest_quotes, self.settings.alert_max_quote_age_seconds),
                    "_score_gte_70": score_counts.get("gte_70", 0),
                    "_alerts_1h": alerts_1h,
                },
            )
    async def _history_bootstrap_loop(self) -> None:
        while not self._stop.is_set():
            results: dict[str, int] = {}
            try:
                async with CoinbaseRestClient(self.settings) as rest:
                    semaphore = asyncio.Semaphore(self.settings.bootstrap_concurrency)

                    async def run_one(product_id: str) -> None:
                        async with semaphore:
                            results[product_id] = await self._bootstrap_product_history_incremental(rest, product_id)

                    await asyncio.gather(*(run_one(product_id) for product_id in list(self.products)))
                async with session_scope(self.settings) as session:
                    await StatusRepository(session).set_status(
                        "history_bootstrap",
                        {"last_run_at": utc_now().isoformat(), "products": len(results), "candles": sum(results.values())},
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("history_bootstrap_loop_failed", extra={"_error_type": type(exc).__name__})
            await asyncio.sleep(3600)

    async def _bootstrap_product_history_incremental(self, rest: CoinbaseRestClient, product_id: str) -> int:
        now = utc_now()
        total = 0
        ranges = [
            ("ONE_MINUTE", now - timedelta(hours=24), now),
            ("FIVE_MINUTE", now - timedelta(days=min(self.settings.bootstrap_history_days, 7)), now),
            ("ONE_HOUR", now - timedelta(days=self.settings.bootstrap_history_days), now),
        ]
        for granularity, start, end in ranges:
            try:
                async for candles in rest.iter_candle_ranges(product_id, start, end, granularity=granularity):
                    async with session_scope(self.settings) as session:
                        total += await MarketRepository(session).upsert_candles(candles, source="coinbase-rest")
            except Exception as exc:  # noqa: BLE001
                logger.warning("history_bootstrap_product_failed", extra={"_product_id": product_id, "_granularity": granularity, "_error_type": type(exc).__name__})
        if total:
            logger.info("history_bootstrapped", extra={"_product_id": product_id, "_candles": total})
        return total


def summarize_ws_status(value: dict[str, Any]) -> dict[str, Any]:
    events = value.get("events") if isinstance(value, dict) else None
    if isinstance(events, list):
        return {"event_count": len(events), "timestamp": value.get("timestamp")}
    if isinstance(value, dict) and "products" in value:
        products = value.get("products") or []
        return {"product_count": len(products), "type": value.get("type")}
    return value if isinstance(value, dict) else {"value": str(value)}

def qualifies(candidate: Candidate, settings: Settings) -> bool:
    return not qualification_blockers(candidate, settings)


def choose_status(base_status: SetupStatus, catalyst_score: float, extension_24h_pct: float | None) -> SetupStatus:
    if catalyst_score >= 85.0:
        return SetupStatus.FAST_MOVE
    return base_status


def build_tags(
    product: ProductInfo,
    cap: MarketCapMetrics,
    volume: Any,
    structure: Any,
    score: Any,
    microcap_threshold: float,
    catalyst: dict[str, Any] | None,
) -> list[str]:
    tags = microcap_tags(cap, microcap_threshold)
    if volume.ratios.get("5m_vs_baseline", 0.0) >= 2.0 or volume.ratios.get("15m_vs_baseline", 0.0) >= 2.0:
        tags.append("volume_acceleration")
    if structure.compression_score >= 65.0:
        tags.append("compression")
    if score.components.get("OrderBookScore", 0.0) >= 60.0:
        tags.append("order_book")
    if catalyst:
        tags.append(str(catalyst.get("source", "catalyst")))
    if cap.circulating_market_cap and cap.circulating_market_cap <= microcap_threshold and structure.compression_score >= 60.0:
        tags.append("dormant_microcap")
    return list(dict.fromkeys(tags))


def _catalyst_map(rows: list[Any]) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for row in rows:
        data = {
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
        for asset in row.affected_assets:
            current = mapped.get(str(asset).upper())
            if current is None or float(data["strength"] or 0.0) > float(current.get("strength") or 0.0):
                mapped[str(asset).upper()] = data
    return mapped


async def _put_latest(queue: asyncio.Queue[Any], value: Any) -> None:
    try:
        queue.put_nowait(value)
    except asyncio.QueueFull:
        _ = queue.get_nowait()
        queue.put_nowait(value)


def drain_queue(queue: asyncio.Queue[Any], limit: int) -> list[Any]:
    items: list[Any] = []
    for _ in range(limit):
        try:
            items.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return items


def isoformat(value: Any | None) -> str | None:
    return value.isoformat() if value is not None else None


def count_recent_quotes(quotes: dict[str, LiveQuote], max_age_seconds: float) -> int:
    return sum(
        1
        for quote in quotes.values()
        if quote.quote_age_seconds is not None and quote.quote_age_seconds <= max_age_seconds
    )


def heartbeat_quote_age(quotes: dict[str, LiveQuote]) -> str:
    ages = [quote.quote_age_seconds for quote in quotes.values() if quote.quote_age_seconds is not None]
    if not ages:
        return "n/a"
    return f"{min(ages):.1f} sec"


def format_near_miss(row: Any | None) -> str:
    if not isinstance(row, dict):
        return "None"
    reasons = row.get("suppression_reasons") or row.get("warnings") or ["n/a"]
    score = row.get("early_move_score")
    score_text = f"{score:.0f}" if isinstance(score, (int, float)) else "n/a"
    return f"{row.get('symbol', 'n/a')} - score {score_text}; failed because: {reasons[0]}"

def diagnostic_quote_age(diagnostics: list[Any], product_id: str) -> float | None:
    for item in diagnostics:
        if item.candidate.product_id == product_id:
            return item.quote_age_seconds
    return None