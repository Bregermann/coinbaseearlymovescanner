import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.analysis.fibonacci import analyze_fibonacci
from app.analysis.portfolio_rotation import apply_portfolio_rotation
from app.config import Settings
from app.portfolio import load_portfolio
from app.types import (
    BottomMetrics,
    BottomStage,
    CandlePoint,
    Candidate,
    FibMetrics,
    LiquidityMetrics,
    LiveQuote,
    MarketCapMetrics,
    PortfolioHolding,
    RotationClassification,
    ScoreBreakdown,
    SetupStatus,
    StructureMetrics,
    TargetPlan,
    VolumeMetrics,
)


def impulse_series() -> tuple[list[CandlePoint], datetime]:
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows: list[CandlePoint] = []
    previous = 1.05
    for index in range(120):
        if index < 10:
            close = 1.05
        elif index == 10:
            close = 0.98
        elif index <= 45:
            close = 0.98 + (index - 10) / 35 * 1.02
        elif index <= 80:
            close = 2.00 - (index - 45) / 35 * 0.64
        else:
            close = 1.36 + ((index % 6) - 3) * 0.002
        high = max(previous, close) * 1.004
        low = min(previous, close) * 0.996
        if index == 10:
            low = 0.96
        if index == 45:
            high = 2.02
        if index == 119:
            low = 1.34
            close = 1.37
        quote_volume = 5_000.0 if 43 <= index <= 46 else (550.0 if index > 80 else 1_000.0)
        rows.append(
            CandlePoint(
                product_id="FIB-USD",
                start=start + timedelta(hours=index),
                granularity_seconds=3600,
                open=previous,
                high=high,
                low=low,
                close=close,
                volume=quote_volume / close,
                quote_volume=quote_volume,
            )
        )
        previous = close
    return rows, start + timedelta(hours=120)


def structure(price: float = 1.37, support: float = 1.35) -> StructureMetrics:
    return StructureMetrics(
        status=SetupStatus.EARLY,
        base_low=support,
        base_high=1.42,
        support_low=support,
        support_high=support * 1.02,
        resistance=1.50,
        breakout_trigger=1.51,
        confirmation=1.53,
        invalidation=support * 0.96,
        distance_from_base_pct=2.0,
        distance_from_resistance_pct=9.5,
        highs={"30d": 2.02},
        compression_score=75.0,
        higher_low_score=70.0,
        base_proximity_score=82.0,
        resistance_proximity_score=65.0,
        extension_24h_pct=3.0,
    )


def bottom(stage: BottomStage, relative_strength: float = 75.0, breakout_score: float = 75.0) -> BottomMetrics:
    return BottomMetrics(
        stage=stage,
        pattern="Fib base",
        bottom_score=82.0,
        breakout_score=breakout_score,
        prior_drawdown_pct=-32.0,
        prior_impulse_pct=65.0,
        support=1.35,
        resistance=1.50,
        invalidation=1.30,
        distance_to_breakout_pct=9.5,
        atr_compression_ratio=0.65,
        bollinger_width_ratio=0.60,
        volume_dryup_ratio=0.55,
        first_expansion_ratio=1.8,
        relative_strength_score=relative_strength,
        macd_state="Bullish curl",
        rsi_state="Bullish divergence",
        bollinger_state="Compressed",
        relative_strength_state="Improving vs BTC/ETH",
    )


def test_fibonacci_selects_meaningful_closed_candle_impulse_and_levels():
    candles, now = impulse_series()
    result = analyze_fibonacci(
        candles,
        1.37,
        now,
        structure=structure(),
        bottom=bottom(BottomStage.PRE_BREAKOUT),
        minimum_confidence=50.0,
    )

    assert result.reliable
    assert result.anchor_low is not None and result.anchor_high is not None
    assert result.anchor_high > result.anchor_low * 1.5
    assert result.retracements["0.618"] < result.retracements["0.500"]
    assert result.extensions["1.618"] > result.anchor_high
    assert result.confluence_score >= 50.0
    assert result.probability_confidence == "insufficient historical sample"


def test_fibonacci_never_uses_unclosed_future_candle():
    candles, now = impulse_series()
    baseline = analyze_fibonacci(candles, 1.37, now, structure=structure(), minimum_confidence=50.0)
    future = CandlePoint(
        "FIB-USD", now, 3600, 1.37, 8.0, 1.30, 7.0, 1_000_000.0, 7_000_000.0
    )
    with_future = analyze_fibonacci(candles + [future], 1.37, now, structure=structure(), minimum_confidence=50.0)

    assert with_future.anchor_low == baseline.anchor_low
    assert with_future.anchor_high == baseline.anchor_high
    assert with_future.signal == baseline.signal


def make_candidate(
    symbol: str,
    opportunity_score: float,
    *,
    price: float,
    status: SetupStatus = SetupStatus.EARLY,
    bottom_stage: BottomStage = BottomStage.PRE_BREAKOUT,
    volume_score: float = 80.0,
    extension_penalty: float = 0.0,
    already_pumped_penalty: float = 0.0,
    target: float | None = None,
    invalidation: float | None = None,
    fib_extension: float | None = None,
) -> Candidate:
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    support = invalidation * 1.04 if invalidation else price * 0.92
    metrics = structure(price, support)
    metrics.status = status
    metrics.invalidation = invalidation or price * 0.90
    metrics.extension_24h_pct = 25.0 if status == SetupStatus.ALREADY_EXTENDED else 4.0
    volume = VolumeMetrics(
        volume_quote={"24h": 20_000_000.0},
        ratios={"5m_vs_baseline": volume_score / 20.0, "15m_vs_baseline": volume_score / 20.0},
        volume_acceleration=4.0,
        price_acceleration=1.0,
        volume_vs_price_acceleration=4.0,
    )
    fib = FibMetrics(
        reliable=True,
        anchor_low=price * 0.70,
        anchor_high=price * 1.20,
        anchor_timestamp_low=now - timedelta(days=3),
        anchor_timestamp_high=now - timedelta(days=1),
        timeframe="1h",
        swing_confidence=86.0,
        retracements={"0.618": price * 0.98},
        extensions={"1.272": target or price * 1.20, "1.618": fib_extension or price * 1.45},
        nearest_support=price * 0.98,
        nearest_resistance=target or price * 1.20,
        signal="0.618 SUPPORT CONFIRMED",
        confluence_score=90.0,
    )
    score = ScoreBreakdown(
        technical_score=opportunity_score,
        catalyst_score=0.0,
        early_move_score=opportunity_score,
        components={
            "RiskAdjustedOpportunityScore": opportunity_score,
            "UpsideRunwayScore": 85.0,
            "LiquiditySafetyScore": 88.0,
            "VolumeAccelerationScore": volume_score,
            "OrderBookScore": 82.0,
        },
        penalties={
            "ExtensionPenalty": extension_penalty,
            "AlreadyPumpedPenalty": already_pumped_penalty,
        },
        reasons=[],
        risks=[],
    )
    candidate_bottom = bottom(bottom_stage, relative_strength=78.0, breakout_score=82.0)
    candidate_bottom.support = support
    candidate_bottom.invalidation = metrics.invalidation
    return Candidate(
        product_id=f"{symbol}-USD",
        symbol=symbol,
        status=status,
        quote=LiveQuote(f"{symbol}-USD", price, price * 0.999, price * 1.001, None, 4.0, now, now, now),
        volume=volume,
        structure=metrics,
        liquidity=LiquidityMetrics(0.06, {"1": 1_000_000.0}, {"1": 1_000_000.0}, 0.1, 20.0, 82.0),
        market_cap=MarketCapMetrics(None, None, None, 700_000_000.0, 500_000_000.0, 4.0),
        targets=TargetPlan(target or price * 1.20, price * 1.35, price * 1.50, "structure"),
        score=score,
        bottom=candidate_bottom,
        fib=fib,
    )


def test_rotation_recommends_only_partial_trim_of_cooling_holding():
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    destination = make_candidate(
        "NEW", 91.0, price=1.0, bottom_stage=BottomStage.BREAKOUT_FIRING, target=1.20, invalidation=0.90
    )
    cooling = make_candidate(
        "COOL",
        62.0,
        price=2.0,
        status=SetupStatus.ALREADY_EXTENDED,
        bottom_stage=BottomStage.NONE,
        volume_score=18.0,
        extension_penalty=24.0,
        already_pumped_penalty=38.0,
        target=2.10,
        invalidation=1.70,
        fib_extension=1.90,
    )
    holdings = [PortfolioHolding("COOL", quantity=500.0)]

    apply_portfolio_rotation([destination, cooling], holdings, Settings(), now=now)

    assert destination.rotation is not None
    assert destination.rotation.classification in {
        RotationClassification.PARTIAL_ROTATION,
        RotationClassification.STRONG_ROTATION,
    }
    assert 10.0 <= destination.rotation.suggested_percentage <= 35.0
    assert destination.rotation.best_source.ticker == "COOL"


def test_rotation_protects_relative_strength_leader():
    destination = make_candidate("NEW", 94.0, price=1.0, target=1.25, invalidation=0.90)
    leader = make_candidate(
        "LEAD", 88.0, price=2.0, bottom_stage=BottomStage.BREAKOUT_FIRING, target=2.30, invalidation=1.85
    )

    apply_portfolio_rotation(
        [destination, leader],
        [PortfolioHolding("LEAD", quantity=100.0)],
        Settings(),
    )

    assert destination.rotation.best_source.protected
    assert destination.rotation.classification == RotationClassification.NO_ROTATION
    assert "ROTATION_PROTECTED" in destination.rotation.best_source.reason


def test_rotation_cooldown_blocks_repeat_pair():
    now = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    destination = make_candidate(
        "NEW", 91.0, price=1.0, bottom_stage=BottomStage.BREAKOUT_FIRING, target=1.20, invalidation=0.90
    )
    cooling = make_candidate(
        "COOL", 60.0, price=2.0, status=SetupStatus.ALREADY_EXTENDED,
        bottom_stage=BottomStage.NONE, volume_score=15.0, extension_penalty=25.0,
        already_pumped_penalty=40.0, target=2.10, invalidation=1.70, fib_extension=1.90,
    )
    recent = SimpleNamespace(
        source_asset="COOL",
        destination_asset="NEW",
        created_at=now - timedelta(minutes=30),
    )

    apply_portfolio_rotation(
        [destination, cooling],
        [PortfolioHolding("COOL", quantity=100.0)],
        Settings(),
        recent_recommendations=[recent],
        now=now,
    )

    assert destination.rotation.classification == RotationClassification.NO_ROTATION
    assert "cooldown" in destination.rotation.best_source.reason


def test_private_portfolio_config_accepts_missing_cost_basis(tmp_path):
    path = tmp_path / "portfolio.json"
    path.write_text('{"holdings":[{"ticker":"abc","quantity":12.5}]}', encoding="utf-8")

    holdings = load_portfolio(Settings(portfolio_file=str(path)))

    assert holdings == [PortfolioHolding(ticker="ABC", quantity=12.5, cost_basis=None, current_position_value=None)]


def test_compact_discord_payload_and_rotation_record_are_serializable():
    destination = make_candidate(
        "NEW", 91.0, price=1.0, bottom_stage=BottomStage.BREAKOUT_FIRING, target=1.20, invalidation=0.90
    )
    cooling = make_candidate(
        "COOL", 60.0, price=2.0, status=SetupStatus.ALREADY_EXTENDED,
        bottom_stage=BottomStage.NONE, volume_score=15.0, extension_penalty=25.0,
        already_pumped_penalty=40.0, target=2.10, invalidation=1.70, fib_extension=1.90,
    )
    apply_portfolio_rotation(
        [destination, cooling],
        [PortfolioHolding("COOL", quantity=100.0)],
        Settings(),
    )

    payload = candidate_to_payload(destination, quote_age_seconds=0.4)
    encoded = json.dumps(payload)
    record = rotation_to_record(destination, discord_message_id="diagnostic-id")

    assert len(encoded) < 6_000
    assert all(len(field["value"]) <= 1_024 for field in payload["embeds"][0]["fields"])
    assert record is not None
    json.dumps(record["raw"])
from app.alerts.formatter import candidate_to_payload, rotation_to_record
