from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class SetupStatus(StrEnum):
    EARLY = "EARLY"
    CONFIRMING = "CONFIRMING"
    FAST_MOVE = "FAST-MOVE"
    ALREADY_EXTENDED = "ALREADY EXTENDED"


class FreshnessState(StrEnum):
    IDEAL = "IDEAL"
    WARNING = "WARNING"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class ProductInfo:
    product_id: str
    base_currency: str
    quote_currency: str
    price: float | None = None
    volume_24h: float | None = None
    quote_volume_24h: float | None = None
    status: str | None = None
    trading_disabled: bool = False
    cancel_only: bool = False
    limit_only: bool = False
    post_only: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LiveQuote:
    product_id: str
    price: float
    best_bid: float | None
    best_ask: float | None
    volume_24h: float | None
    price_change_24h_pct: float | None
    exchange_time: datetime | None
    ingestion_time: datetime
    last_trade_time: datetime | None = None

    @property
    def quote_age_seconds(self) -> float | None:
        reference_time = datetime.now(timezone.utc)
        market_time = self.exchange_time or self.last_trade_time or self.ingestion_time
        if market_time is None:
            return None
        if market_time.tzinfo is None:
            market_time = market_time.replace(tzinfo=timezone.utc)
        return max(0.0, (reference_time - market_time.astimezone(timezone.utc)).total_seconds())


@dataclass(slots=True)
class CandlePoint:
    product_id: str
    start: datetime
    granularity_seconds: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float


@dataclass(slots=True)
class VolumeMetrics:
    volume_quote: dict[str, float]
    ratios: dict[str, float]
    volume_acceleration: float
    price_acceleration: float
    volume_vs_price_acceleration: float


@dataclass(slots=True)
class StructureMetrics:
    status: SetupStatus
    base_low: float | None
    base_high: float | None
    support_low: float | None
    support_high: float | None
    resistance: float | None
    breakout_trigger: float | None
    confirmation: float | None
    invalidation: float | None
    distance_from_base_pct: float | None
    distance_from_resistance_pct: float | None
    highs: dict[str, float]
    compression_score: float
    higher_low_score: float
    base_proximity_score: float
    resistance_proximity_score: float
    extension_24h_pct: float | None


@dataclass(slots=True)
class LiquidityMetrics:
    spread_pct: float | None
    bid_depth_usd: dict[str, float]
    ask_depth_usd: dict[str, float]
    imbalance: float | None
    liquidity_vacuum_score: float
    order_book_score: float


@dataclass(slots=True)
class MarketCapMetrics:
    circulating_supply: float | None
    total_supply: float | None
    max_supply: float | None
    fdv: float | None
    circulating_market_cap: float | None
    volume_to_market_cap: float | None
    coingecko_id: str | None = None


@dataclass(slots=True)
class TargetPlan:
    tp1: float | None
    tp2: float | None
    stretch: float | None
    rationale: str


@dataclass(slots=True)
class ScoreBreakdown:
    technical_score: float
    catalyst_score: float
    early_move_score: float
    components: dict[str, float]
    penalties: dict[str, float]
    reasons: list[str]
    risks: list[str]


@dataclass(slots=True)
class Candidate:
    product_id: str
    symbol: str
    status: SetupStatus
    quote: LiveQuote
    volume: VolumeMetrics
    structure: StructureMetrics
    liquidity: LiquidityMetrics
    market_cap: MarketCapMetrics
    targets: TargetPlan
    score: ScoreBreakdown
    catalyst: dict[str, Any] | None = None
    tags: list[str] = field(default_factory=list)
