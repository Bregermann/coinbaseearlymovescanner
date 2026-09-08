from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.timeutils import utc_now


class Base(DeclarativeBase):
    pass


class ProductModel(Base):
    __tablename__ = "products"

    product_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    canonical_asset: Mapped[str] = mapped_column(String(32), index=True)
    base_currency: Mapped[str] = mapped_column(String(32), index=True)
    quote_currency: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trading_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_only: Mapped[bool] = mapped_column(Boolean, default=False)
    limit_only: Mapped[bool] = mapped_column(Boolean, default=False)
    post_only: Mapped[bool] = mapped_column(Boolean, default=False)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    quote_volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class MarketTickModel(Base):
    __tablename__ = "market_ticks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(String(64), index=True)
    price: Mapped[float] = mapped_column(Float)
    best_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_change_24h_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    exchange_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    ingestion_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    last_trade_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quote_age_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)


class CandleModel(Base):
    __tablename__ = "candles"
    __table_args__ = (UniqueConstraint("product_id", "start", "granularity_seconds", name="uq_candle"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(String(64), index=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    granularity_seconds: Mapped[int] = mapped_column(Integer, default=60, index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    quote_volume: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32), default="coinbase")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class OrderBookSnapshotModel(Base):
    __tablename__ = "order_book_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(String(64), index=True)
    exchange_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_depth_0_25_pct: Mapped[float] = mapped_column(Float, default=0.0)
    bid_depth_0_5_pct: Mapped[float] = mapped_column(Float, default=0.0)
    bid_depth_1_pct: Mapped[float] = mapped_column(Float, default=0.0)
    bid_depth_2_pct: Mapped[float] = mapped_column(Float, default=0.0)
    bid_depth_5_pct: Mapped[float] = mapped_column(Float, default=0.0)
    ask_depth_0_25_pct: Mapped[float] = mapped_column(Float, default=0.0)
    ask_depth_0_5_pct: Mapped[float] = mapped_column(Float, default=0.0)
    ask_depth_1_pct: Mapped[float] = mapped_column(Float, default=0.0)
    ask_depth_2_pct: Mapped[float] = mapped_column(Float, default=0.0)
    ask_depth_5_pct: Mapped[float] = mapped_column(Float, default=0.0)
    imbalance: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_vacuum_score: Mapped[float] = mapped_column(Float, default=0.0)
    raw_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class AssetMetadataModel(Base):
    __tablename__ = "asset_metadata"

    asset: Mapped[str] = mapped_column(String(32), primary_key=True)
    coingecko_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    circulating_supply: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_supply: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_supply: Mapped[float | None] = mapped_column(Float, nullable=True)
    fdv: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class CatalystModel(Base):
    __tablename__ = "catalysts"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_catalyst_source_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(256), index=True)
    headline: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    announcement_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    first_detected_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    affected_assets: Mapped[list[str]] = mapped_column(JSON, default=list)
    catalyst_type: Mapped[str] = mapped_column(String(64), index=True)
    strength: Mapped[float] = mapped_column(Float, default=0.0)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class CandidateSnapshotModel(Base):
    __tablename__ = "candidate_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    early_move_score: Mapped[float] = mapped_column(Float)
    technical_score: Mapped[float] = mapped_column(Float)
    catalyst_score: Mapped[float] = mapped_column(Float)
    component_scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    penalties: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    risks: Mapped[list[str]] = mapped_column(JSON, default=list)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)


class AlertModel(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    alert_type: Mapped[str] = mapped_column(String(64), index=True)
    detection_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    detection_price: Mapped[float] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float)
    component_scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    risks: Mapped[list[str]] = mapped_column(JSON, default=list)
    support_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    support_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    invalidation_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp1: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp2: Mapped[float | None] = mapped_column(Float, nullable=True)
    stretch: Mapped[float | None] = mapped_column(Float, nullable=True)
    catalyst_id: Mapped[int | None] = mapped_column(ForeignKey("catalysts.id"), nullable=True)
    discord_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    closed_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_candidate: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    observations: Mapped[list["AlertObservationModel"]] = relationship(back_populates="alert")


class AlertObservationModel(Base):
    __tablename__ = "alert_observations"
    __table_args__ = (UniqueConstraint("alert_id", "horizon", name="uq_alert_horizon"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id"), index=True)
    horizon: Mapped[str] = mapped_column(String(32), index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mfe_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp1_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    tp2_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    stretch_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    invalidation_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    invalidation_before_tp1: Mapped[bool] = mapped_column(Boolean, default=False)

    alert: Mapped[AlertModel] = relationship(back_populates="observations")


class RotationRecommendationModel(Base):
    __tablename__ = "rotation_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    source_asset: Mapped[str] = mapped_column(String(32), index=True)
    destination_asset: Mapped[str] = mapped_column(String(32), index=True)
    classification: Mapped[str] = mapped_column(String(32), index=True)
    suggested_percentage: Mapped[float] = mapped_column(Float)
    source_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    destination_price: Mapped[float] = mapped_column(Float)
    source_score: Mapped[float] = mapped_column(Float)
    destination_score: Mapped[float] = mapped_column(Float)
    rotation_advantage: Mapped[float] = mapped_column(Float)
    source_risk_reward: Mapped[float | None] = mapped_column(Float, nullable=True)
    destination_risk_reward: Mapped[float | None] = mapped_column(Float, nullable=True)
    fib_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    discord_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    observations: Mapped[list["RotationObservationModel"]] = relationship(back_populates="recommendation")


class RotationObservationModel(Base):
    __tablename__ = "rotation_observations"
    __table_args__ = (UniqueConstraint("recommendation_id", "horizon", name="uq_rotation_horizon"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recommendation_id: Mapped[int] = mapped_column(ForeignKey("rotation_recommendations.id"), index=True)
    horizon: Mapped[str] = mapped_column(String(32), index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    source_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    destination_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    destination_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    relative_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    destination_outperformed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    recommendation: Mapped[RotationRecommendationModel] = relationship(back_populates="observations")


class HistoryBootstrapStateModel(Base):
    __tablename__ = "history_bootstrap_state"

    product_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    granularity_seconds: Mapped[int] = mapped_column(Integer, primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    earliest_candle_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latest_candle_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScannerStatusModel(Base):
    __tablename__ = "scanner_status"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
