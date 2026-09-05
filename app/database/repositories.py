from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import Select, and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AlertModel,
    AlertObservationModel,
    AssetMetadataModel,
    CandleModel,
    CandidateSnapshotModel,
    CatalystModel,
    MarketTickModel,
    OrderBookSnapshotModel,
    ProductModel,
    ScannerStatusModel,
)
from app.timeutils import utc_now
from app.types import CandlePoint, LiveQuote, MarketCapMetrics, ProductInfo


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _jsonable_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return _ensure_utc(value).isoformat() if _ensure_utc(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


class ProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_products(self, products: Iterable[ProductInfo]) -> None:
        now = utc_now()
        for product in products:
            model = await self.session.get(ProductModel, product.product_id)
            if model is None:
                model = ProductModel(
                    product_id=product.product_id,
                    canonical_asset=product.base_currency.upper(),
                    base_currency=product.base_currency.upper(),
                    quote_currency=product.quote_currency.upper(),
                    first_seen_at=now,
                )
                self.session.add(model)
            model.status = product.status
            model.trading_disabled = product.trading_disabled
            model.cancel_only = product.cancel_only
            model.limit_only = product.limit_only
            model.post_only = product.post_only
            model.last_price = product.price
            model.volume_24h = product.volume_24h
            model.quote_volume_24h = product.quote_volume_24h
            model.raw = product.raw
            model.last_seen_at = now

    async def list_active_products(self) -> list[ProductModel]:
        result = await self.session.execute(
            select(ProductModel)
            .where(ProductModel.trading_disabled.is_(False))
            .order_by(ProductModel.canonical_asset.asc())
        )
        return list(result.scalars())

    async def get_by_asset(self, asset: str) -> ProductModel | None:
        result = await self.session.execute(
            select(ProductModel)
            .where(ProductModel.canonical_asset == asset.upper())
            .order_by(ProductModel.quote_currency.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()


class MarketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_tick(self, quote: LiveQuote) -> MarketTickModel:
        model = MarketTickModel(
            product_id=quote.product_id,
            price=quote.price,
            best_bid=quote.best_bid,
            best_ask=quote.best_ask,
            volume_24h=quote.volume_24h,
            price_change_24h_pct=quote.price_change_24h_pct,
            exchange_time=quote.exchange_time,
            ingestion_time=quote.ingestion_time,
            last_trade_time=quote.last_trade_time,
            quote_age_seconds=quote.quote_age_seconds,
        )
        self.session.add(model)
        return model

    async def latest_quote(self, product_id: str) -> LiveQuote | None:
        result = await self.session.execute(
            select(MarketTickModel)
            .where(MarketTickModel.product_id == product_id)
            .order_by(desc(MarketTickModel.ingestion_time))
            .limit(1)
        )
        tick = result.scalar_one_or_none()
        if tick is None:
            return None
        return LiveQuote(
            product_id=tick.product_id,
            price=tick.price,
            best_bid=tick.best_bid,
            best_ask=tick.best_ask,
            volume_24h=tick.volume_24h,
            price_change_24h_pct=tick.price_change_24h_pct,
            exchange_time=_ensure_utc(tick.exchange_time),
            ingestion_time=_ensure_utc(tick.ingestion_time) or tick.ingestion_time,
            last_trade_time=_ensure_utc(tick.last_trade_time),
        )

    async def latest_quotes(self) -> dict[str, LiveQuote]:
        subq = (
            select(MarketTickModel.product_id, func.max(MarketTickModel.ingestion_time).label("max_time"))
            .group_by(MarketTickModel.product_id)
            .subquery()
        )
        result = await self.session.execute(
            select(MarketTickModel).join(
                subq,
                and_(
                    MarketTickModel.product_id == subq.c.product_id,
                    MarketTickModel.ingestion_time == subq.c.max_time,
                ),
            )
        )
        quotes: dict[str, LiveQuote] = {}
        for tick in result.scalars():
            quotes[tick.product_id] = LiveQuote(
                product_id=tick.product_id,
                price=tick.price,
                best_bid=tick.best_bid,
                best_ask=tick.best_ask,
                volume_24h=tick.volume_24h,
                price_change_24h_pct=tick.price_change_24h_pct,
                exchange_time=_ensure_utc(tick.exchange_time),
                ingestion_time=_ensure_utc(tick.ingestion_time) or tick.ingestion_time,
                last_trade_time=_ensure_utc(tick.last_trade_time),
            )
        return quotes

    async def upsert_candle(self, candle: CandlePoint, source: str = "coinbase") -> None:
        result = await self.session.execute(
            select(CandleModel).where(
                CandleModel.product_id == candle.product_id,
                CandleModel.start == candle.start,
                CandleModel.granularity_seconds == candle.granularity_seconds,
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            model = CandleModel(
                product_id=candle.product_id,
                start=candle.start,
                granularity_seconds=candle.granularity_seconds,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                quote_volume=candle.quote_volume,
                source=source,
            )
            self.session.add(model)
        else:
            model.open = candle.open
            model.high = candle.high
            model.low = candle.low
            model.close = candle.close
            model.volume = candle.volume
            model.quote_volume = candle.quote_volume
            model.source = source
            model.updated_at = utc_now()

    async def upsert_candles(self, candles: Iterable[CandlePoint], source: str = "coinbase") -> int:
        count = 0
        for candle in candles:
            await self.upsert_candle(candle, source=source)
            count += 1
        return count

    async def recent_candles(self, product_id: str, since: datetime, limit: int | None = None) -> list[CandlePoint]:
        stmt: Select[tuple[CandleModel]] = (
            select(CandleModel)
            .where(CandleModel.product_id == product_id, CandleModel.start >= since)
            .order_by(CandleModel.start.asc())
        )
        if limit:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return [
            CandlePoint(
                product_id=row.product_id,
                start=_ensure_utc(row.start) or row.start,
                granularity_seconds=row.granularity_seconds,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                quote_volume=row.quote_volume,
            )
            for row in result.scalars()
        ]

    async def insert_order_book_summary(self, product_id: str, summary: dict[str, Any]) -> None:
        bid = summary.get("bid_depth_usd", {}) or {}
        ask = summary.get("ask_depth_usd", {}) or {}
        model = OrderBookSnapshotModel(
            product_id=product_id,
            exchange_time=_ensure_utc(summary.get("exchange_time")),
            captured_at=_ensure_utc(summary.get("captured_at")) or utc_now(),
            spread_pct=summary.get("spread_pct"),
            bid_depth_0_25_pct=float(bid.get("0.25", 0.0)),
            bid_depth_0_5_pct=float(bid.get("0.5", 0.0)),
            bid_depth_1_pct=float(bid.get("1", 0.0)),
            bid_depth_2_pct=float(bid.get("2", 0.0)),
            bid_depth_5_pct=float(bid.get("5", 0.0)),
            ask_depth_0_25_pct=float(ask.get("0.25", 0.0)),
            ask_depth_0_5_pct=float(ask.get("0.5", 0.0)),
            ask_depth_1_pct=float(ask.get("1", 0.0)),
            ask_depth_2_pct=float(ask.get("2", 0.0)),
            ask_depth_5_pct=float(ask.get("5", 0.0)),
            imbalance=summary.get("imbalance"),
            liquidity_vacuum_score=float(summary.get("liquidity_vacuum_score", 0.0)),
            raw_summary=_json_safe(summary),
        )
        self.session.add(model)

    async def latest_order_book_summary(self, product_id: str) -> OrderBookSnapshotModel | None:
        result = await self.session.execute(
            select(OrderBookSnapshotModel)
            .where(OrderBookSnapshotModel.product_id == product_id)
            .order_by(desc(OrderBookSnapshotModel.captured_at))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def highs_since_windows(self, product_id: str, windows: dict[str, datetime]) -> dict[str, float]:
        highs: dict[str, float] = {}
        for label, since in windows.items():
            result = await self.session.execute(
                select(func.max(CandleModel.high)).where(
                    CandleModel.product_id == product_id,
                    CandleModel.start >= since,
                )
            )
            value = result.scalar_one_or_none()
            if value is not None:
                highs[label] = float(value)
        return highs


class MetadataRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_metadata(self, asset: str, metrics: MarketCapMetrics, raw: dict[str, Any] | None = None) -> None:
        asset = asset.upper()
        model = await self.session.get(AssetMetadataModel, asset)
        if model is None:
            model = AssetMetadataModel(asset=asset)
            self.session.add(model)
        model.coingecko_id = metrics.coingecko_id
        model.circulating_supply = metrics.circulating_supply
        model.total_supply = metrics.total_supply
        model.max_supply = metrics.max_supply
        model.fdv = metrics.fdv
        model.raw = raw
        model.refreshed_at = utc_now()

    async def get_metadata(self, asset: str) -> AssetMetadataModel | None:
        return await self.session.get(AssetMetadataModel, asset.upper())

    async def all_metadata(self) -> dict[str, AssetMetadataModel]:
        result = await self.session.execute(select(AssetMetadataModel))
        return {row.asset: row for row in result.scalars()}


class CatalystRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_if_new(self, data: dict[str, Any]) -> CatalystModel | None:
        result = await self.session.execute(
            select(CatalystModel).where(
                CatalystModel.source == data["source"],
                CatalystModel.external_id == data["external_id"],
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            return None
        model = CatalystModel(**data)
        self.session.add(model)
        await self.session.flush()
        return model

    async def fresh_catalysts(self, since: datetime) -> list[CatalystModel]:
        result = await self.session.execute(
            select(CatalystModel)
            .where(CatalystModel.first_detected_time >= since)
            .order_by(desc(CatalystModel.first_detected_time))
        )
        return list(result.scalars())

    async def catalysts_for_asset(self, asset: str, since: datetime) -> list[CatalystModel]:
        result = await self.session.execute(
            select(CatalystModel)
            .where(CatalystModel.first_detected_time >= since)
            .order_by(desc(CatalystModel.first_detected_time))
        )
        asset = asset.upper()
        return [row for row in result.scalars() if asset in {str(a).upper() for a in row.affected_assets}]


class AlertRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_candidate_snapshot(self, data: dict[str, Any]) -> CandidateSnapshotModel:
        model = CandidateSnapshotModel(**data)
        self.session.add(model)
        await self.session.flush()
        return model

    async def active_alert_for_product(self, product_id: str) -> AlertModel | None:
        result = await self.session.execute(
            select(AlertModel)
            .where(AlertModel.product_id == product_id, AlertModel.is_active.is_(True))
            .order_by(desc(AlertModel.detection_time))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def active_alerts_by_product(self) -> dict[str, AlertModel]:
        result = await self.session.execute(
            select(AlertModel)
            .where(AlertModel.is_active.is_(True))
            .order_by(desc(AlertModel.detection_time))
        )
        active: dict[str, AlertModel] = {}
        for alert in result.scalars():
            active.setdefault(alert.product_id, alert)
        return active

    async def create_alert(self, data: dict[str, Any]) -> AlertModel:
        model = AlertModel(**data)
        self.session.add(model)
        await self.session.flush()
        return model

    async def close_alert(self, alert: AlertModel, reason: str) -> None:
        alert.is_active = False
        alert.closed_reason = reason

    async def due_observations(self, now: datetime | None = None) -> list[AlertObservationModel]:
        now = now or utc_now()
        result = await self.session.execute(
            select(AlertObservationModel)
            .where(AlertObservationModel.observed_at.is_(None), AlertObservationModel.due_at <= now)
            .order_by(AlertObservationModel.due_at.asc())
        )
        return list(result.scalars())

    async def create_observation_schedule(self, alert: AlertModel) -> None:
        horizons = {
            "5m": timedelta(minutes=5),
            "15m": timedelta(minutes=15),
            "30m": timedelta(minutes=30),
            "1h": timedelta(hours=1),
            "4h": timedelta(hours=4),
            "12h": timedelta(hours=12),
            "24h": timedelta(hours=24),
            "3d": timedelta(days=3),
            "7d": timedelta(days=7),
        }
        for label, delta in horizons.items():
            self.session.add(AlertObservationModel(alert_id=alert.id, horizon=label, due_at=alert.detection_time + delta))

    async def alerts_since(self, since: datetime) -> list[AlertModel]:
        result = await self.session.execute(
            select(AlertModel).where(AlertModel.detection_time >= since).order_by(desc(AlertModel.detection_time))
        )
        return list(result.scalars())

    async def observations_for_alerts(self, alert_ids: list[int]) -> list[AlertObservationModel]:
        if not alert_ids:
            return []
        result = await self.session.execute(select(AlertObservationModel).where(AlertObservationModel.alert_id.in_(alert_ids)))
        return list(result.scalars())


class StatusRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def set_status(self, key: str, value: dict[str, Any]) -> None:
        model = await self.session.get(ScannerStatusModel, key)
        if model is None:
            model = ScannerStatusModel(key=key)
            self.session.add(model)
        model.value = value
        model.updated_at = utc_now()

    async def get_status(self, key: str) -> dict[str, Any] | None:
        model = await self.session.get(ScannerStatusModel, key)
        return model.value if model else None

    async def all_status(self) -> dict[str, Any]:
        result = await self.session.execute(select(ScannerStatusModel))
        return {row.key: {"value": row.value, "updated_at": _jsonable_dt(row.updated_at)} for row in result.scalars()}
