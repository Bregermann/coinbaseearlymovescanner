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


class BottomStage(StrEnum):
    NONE = "NONE"
    BOTTOM_FORMING = "BOTTOM FORMING"
    PRE_BREAKOUT = "PRE-BREAKOUT"
    BREAKOUT_FIRING = "BREAKOUT FIRING"
    RETEST_HOLD = "RETEST HOLD"


class RotationClassification(StrEnum):
    NO_ROTATION = "NO_ROTATION"
    WATCH_ROTATION = "WATCH_ROTATION"
    PARTIAL_ROTATION = "PARTIAL_ROTATION"
    STRONG_ROTATION = "STRONG_ROTATION"


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
class BottomMetrics:
    stage: BottomStage
    pattern: str
    bottom_score: float
    breakout_score: float
    prior_drawdown_pct: float | None
    prior_impulse_pct: float | None
    support: float | None
    resistance: float | None
    invalidation: float | None
    distance_to_breakout_pct: float | None
    atr_compression_ratio: float | None
    bollinger_width_ratio: float | None
    volume_dryup_ratio: float | None
    first_expansion_ratio: float | None
    relative_strength_score: float
    macd_state: str
    rsi_state: str
    bollinger_state: str
    relative_strength_state: str
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FibMetrics:
    reliable: bool
    anchor_low: float | None
    anchor_high: float | None
    anchor_timestamp_low: datetime | None
    anchor_timestamp_high: datetime | None
    timeframe: str | None
    swing_confidence: float
    retracements: dict[str, float] = field(default_factory=dict)
    extensions: dict[str, float] = field(default_factory=dict)
    current_retracement: float | None = None
    nearest_support: float | None = None
    nearest_resistance: float | None = None
    golden_pocket_low: float | None = None
    golden_pocket_high: float | None = None
    signal: str = "N/A"
    confluence_score: float = 0.0
    tags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    target_probabilities: dict[str, float | None] = field(default_factory=dict)
    probability_confidence: str = "insufficient historical sample"


@dataclass(slots=True)
class PortfolioHolding:
    ticker: str
    quantity: float
    cost_basis: float | None = None
    current_position_value: float | None = None


@dataclass(slots=True)
class HoldAssessment:
    ticker: str
    quantity: float
    current_price: float | None
    current_position_value: float | None
    hold_score: float
    expected_upside_pct: float | None
    risk_reward: float | None
    deterioration_score: float
    protected: bool
    protection_reason: str | None
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RotationSource:
    ticker: str
    classification: RotationClassification
    hold_score: float
    rotation_advantage: float
    suggested_percentage: float
    source_price: float | None
    source_position_value: float | None
    source_risk_reward: float | None
    deterioration_score: float
    confidence: float
    protected: bool
    reason: str


@dataclass(slots=True)
class RotationRecommendation:
    classification: RotationClassification
    destination_ticker: str
    candidate_score: float
    relative_opportunity_score: float
    candidate_risk_reward: float | None
    suggested_percentage: float
    confidence: float
    best_source: RotationSource | None = None
    sources: list[RotationSource] = field(default_factory=list)
    protected_holdings: list[HoldAssessment] = field(default_factory=list)
    reason: str = ""
    fib_state: str = "N/A"


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
    bottom: BottomMetrics | None = None
    fib: FibMetrics | None = None
    rotation: RotationRecommendation | None = None
    fib: FibMetrics | None = None
    rotation: RotationRecommendation | None = None
