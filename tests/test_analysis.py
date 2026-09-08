from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.alerts.deduplication import AlertDeduplicator
from app.analysis.bottoms import analyze_bottom_structure
from app.analysis.liquidity import summarize_depth
from app.analysis.scoring import apply_risk_adjusted_opportunity, market_cap_class, primary_score, score_candidate
from app.analysis.structure import analyze_structure
from app.analysis.targets import generate_targets
from app.analysis.volume import calculate_volume_metrics, sum_quote_volume
from app.config import Settings
from app.diagnostics import qualification_blockers
from app.types import BottomMetrics, BottomStage, Candidate, CandlePoint, LiquidityMetrics, LiveQuote, MarketCapMetrics, SetupStatus, StructureMetrics, TargetPlan, VolumeMetrics


def candle(start, open_price=1.0, high=1.01, low=0.99, close=1.0, quote_volume=100.0, granularity=60):
    return CandlePoint(
        product_id="XYZ-USD",
        start=start,
        granularity_seconds=granularity,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=quote_volume / close,
        quote_volume=quote_volume,
    )


def test_volume_acceleration_detects_short_term_surge_ahead_of_price():
    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    candles = []
    for i in range(120):
        start = now - timedelta(minutes=120 - i)
        qv = 100.0
        if i >= 115:
            qv = 500.0
        candles.append(candle(start, close=1.0 + i * 0.0001, quote_volume=qv))

    metrics = calculate_volume_metrics(candles, now)

    assert metrics.ratios["5m_vs_baseline"] > 4.0
    assert metrics.volume_vs_price_acceleration > 2.0


def test_structure_and_targets_use_observed_levels():
    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    candles = []
    for i in range(180):
        start = now - timedelta(minutes=180 - i)
        high = 1.02
        if i == 90:
            high = 1.18
        if i == 140:
            high = 1.12
        candles.append(candle(start, open_price=1.0, high=high, low=0.98, close=1.01, quote_volume=100.0))

    structure = analyze_structure(candles, 1.01, now)
    targets = generate_targets(1.01, structure)

    assert structure.breakout_trigger is not None
    assert targets.tp1 is not None and targets.tp1 > 1.01
    assert targets.tp2 is not None and targets.tp2 > targets.tp1


def test_liquidity_summary_scores_vacuum_and_spread():
    bids = {0.999: 10_000, 0.995: 20_000, 0.98: 50_000}
    asks = {1.001: 3_000, 1.02: 40_000, 1.04: 60_000}

    summary = summarize_depth(bids, asks)

    assert summary["spread_pct"] < 0.3
    assert summary["bid_depth_usd"]["1"] > summary["ask_depth_usd"]["1"]
    assert summary["liquidity_vacuum_score"] > 50


def test_scoring_penalizes_stale_and_extended_data():
    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    candles = [candle(now - timedelta(minutes=60 - i), close=1.0, quote_volume=100) for i in range(60)]
    volume = calculate_volume_metrics(candles, now)
    structure = analyze_structure(candles, 1.30, now)
    liquidity = LiquidityMetrics(0.1, {}, {}, 0.2, 20.0, 70.0)
    cap = MarketCapMetrics(10_000_000, None, None, None, 10_000_000, 5.0)

    score = score_candidate(volume, structure, liquidity, cap, quote_age_seconds=30.0)

    assert score.penalties["StaleDataPenalty"] >= 35.0
    assert score.early_move_score < 60.0

def synthetic_liquid_volume() -> VolumeMetrics:
    return VolumeMetrics(
        volume_quote={"5m": 120_000.0, "15m": 430_000.0, "1h": 1_900_000.0, "4h": 5_400_000.0, "24h": 18_400_000.0},
        ratios={
            "5m_vs_baseline": 4.5,
            "15m_vs_baseline": 6.2,
            "1h_vs_7d": 4.5,
            "1h_vs_30d": 4.0,
            "4h_vs_7d": 3.2,
            "24h_vs_7d": 1.8,
            "24h_vs_30d": 1.6,
        },
        volume_acceleration=4.8,
        price_acceleration=1.2,
        volume_vs_price_acceleration=4.0,
    )


def synthetic_breakout_structure() -> StructureMetrics:
    return StructureMetrics(
        status=SetupStatus.EARLY,
        base_low=1.00,
        base_high=1.08,
        support_low=1.00,
        support_high=1.04,
        resistance=1.15,
        breakout_trigger=1.16,
        confirmation=1.17,
        invalidation=0.97,
        distance_from_base_pct=4.0,
        distance_from_resistance_pct=9.6,
        highs={"30d": 1.24, "90d": 1.40},
        compression_score=72.0,
        higher_low_score=68.0,
        base_proximity_score=70.0,
        resistance_proximity_score=62.0,
        extension_24h_pct=4.0,
    )


def synthetic_deep_liquidity() -> LiquidityMetrics:
    return LiquidityMetrics(
        spread_pct=0.06,
        bid_depth_usd={"0.5": 620_000.0, "1": 1_250_000.0, "2": 2_700_000.0},
        ask_depth_usd={"0.5": 590_000.0, "1": 1_180_000.0, "2": 2_400_000.0},
        imbalance=0.05,
        liquidity_vacuum_score=35.0,
        order_book_score=86.0,
    )


def test_risk_adjusted_score_prioritizes_liquid_midcap_over_similar_microcap():
    volume = synthetic_liquid_volume()
    structure = synthetic_breakout_structure()
    liquidity = synthetic_deep_liquidity()
    targets = TargetPlan(tp1=1.16, tp2=1.24, stretch=1.40, rationale="observed structure")
    mid_cap = MarketCapMetrics(None, None, None, 800_000_000.0, 500_000_000.0, 3.68)
    micro_cap = MarketCapMetrics(None, None, None, 12_000_000.0, 5_000_000.0, 368.0)

    mid_score = apply_risk_adjusted_opportunity(
        score_candidate(volume, structure, liquidity, mid_cap, quote_age_seconds=1.0),
        volume,
        structure,
        liquidity,
        mid_cap,
        targets,
        price=1.08,
    )
    micro_score = apply_risk_adjusted_opportunity(
        score_candidate(volume, structure, liquidity, micro_cap, quote_age_seconds=1.0),
        volume,
        structure,
        liquidity,
        micro_cap,
        targets,
        price=1.08,
    )

    assert market_cap_class(mid_cap) == "MID CAP"
    assert market_cap_class(micro_cap) == "MICROCAP"
    assert primary_score(mid_score) > primary_score(micro_score)
    assert mid_score.components["LiquiditySafetyScore"] > micro_score.components["LiquiditySafetyScore"]


def test_missing_trader_and_gooner_scores_are_optional_boosters():
    volume = synthetic_liquid_volume()
    structure = synthetic_breakout_structure()
    liquidity = synthetic_deep_liquidity()
    targets = TargetPlan(tp1=1.16, tp2=1.24, stretch=1.40, rationale="observed structure")
    mid_cap = MarketCapMetrics(None, None, None, 800_000_000.0, 500_000_000.0, 3.68)

    score = apply_risk_adjusted_opportunity(
        score_candidate(volume, structure, liquidity, mid_cap, quote_age_seconds=1.0),
        volume,
        structure,
        liquidity,
        mid_cap,
        targets,
        price=1.08,
    )

    assert "TraderAccelerationScore" not in score.components
    assert "GoonerEMAScore" not in score.components
    assert primary_score(score) >= 70.0


def test_bottom_detector_finds_pre_breakout_after_impulse_and_retracement():
    now = datetime(2026, 9, 7, 10, 30, tzinfo=timezone.utc)
    candles = []
    previous = 1.0
    for index in range(240):
        start = now - timedelta(minutes=(240 - index) * 15)
        if index < 36:
            close = 1.0 + index / 35 * 0.50
            volume = 14_000.0
        elif index < 176:
            close = 1.50 - (index - 35) / 140 * 0.43
            volume = 8_000.0 - (index - 36) * 25.0
        else:
            step = index - 176
            amplitude = max(0.004, 0.025 - step * 0.0003)
            close = 1.045 + step * 0.00045 + amplitude * (1 if step % 8 < 4 else -1)
            volume = 2_200.0
            if index >= 235:
                volume = 1_100.0
            if index == 239:
                close = 1.079
                volume = 4_600.0
        high = max(previous, close) * 1.004
        low = min(previous, close) * 0.996
        candles.append(
            candle(
                start,
                open_price=previous,
                high=high,
                low=low,
                close=close,
                quote_volume=volume,
                granularity=900,
            )
        )
        previous = close

    metrics = analyze_bottom_structure(candles, candles[-1].close, now)

    assert metrics.bottom_score >= 68.0
    assert metrics.stage in {BottomStage.PRE_BREAKOUT, BottomStage.BOTTOM_FORMING}
    assert metrics.prior_drawdown_pct is not None and -50.0 <= metrics.prior_drawdown_pct <= -15.0
    assert metrics.support is not None
    assert metrics.resistance is not None


def test_bottom_replay_does_not_see_unclosed_future_candle():
    now = datetime(2026, 9, 7, 10, 30, tzinfo=timezone.utc)
    history = [
        candle(
            now - timedelta(minutes=(80 - index) * 15),
            open_price=1.0,
            high=1.01,
            low=0.99,
            close=1.0,
            quote_volume=1_000.0,
            granularity=900,
        )
        for index in range(80)
    ]
    future = candle(
        now,
        open_price=1.0,
        high=2.0,
        low=0.9,
        close=1.8,
        quote_volume=1_000_000.0,
        granularity=900,
    )

    without_future = analyze_bottom_structure(history, 1.0, now)
    with_future = analyze_bottom_structure(history + [future], 1.0, now)

    assert with_future.bottom_score == without_future.bottom_score
    assert with_future.breakout_score == without_future.breakout_score
    assert with_future.resistance == without_future.resistance


def test_volume_does_not_double_count_overlapping_granularities():
    end = datetime(2026, 9, 7, 10, 30, tzinfo=timezone.utc)
    rows = [
        candle(
            end - timedelta(minutes=5 - index),
            quote_volume=100.0,
            granularity=60,
        )
        for index in range(5)
    ]
    rows.append(
        candle(
            end - timedelta(minutes=5),
            quote_volume=500.0,
            granularity=300,
        )
    )

    assert sum_quote_volume(rows, end, 300) == 500.0


def test_pre_breakout_path_can_alert_before_legacy_threshold_and_transitions():
    now = datetime(2026, 9, 7, 10, 30, tzinfo=timezone.utc)
    settings = Settings(
        min_risk_adjusted_opportunity_score=78.0,
        min_pre_breakout_opportunity_score=70.0,
    )
    volume = synthetic_liquid_volume()
    structure = synthetic_breakout_structure()
    liquidity = synthetic_deep_liquidity()
    cap = MarketCapMetrics(None, None, None, 800_000_000.0, 500_000_000.0, 3.68)
    targets = TargetPlan(1.12, 1.20, 1.35, "observed structure")
    score = score_candidate(volume, structure, liquidity, cap, quote_age_seconds=1.0)
    bottom = BottomMetrics(
        stage=BottomStage.PRE_BREAKOUT,
        pattern="Descending Channel / Bull Flag",
        bottom_score=72.0,
        breakout_score=48.0,
        prior_drawdown_pct=-29.0,
        prior_impulse_pct=42.0,
        support=1.0,
        resistance=1.08,
        invalidation=0.97,
        distance_to_breakout_pct=3.8,
        atr_compression_ratio=0.62,
        bollinger_width_ratio=0.58,
        volume_dryup_ratio=0.60,
        first_expansion_ratio=1.35,
        relative_strength_score=68.0,
        macd_state="Bullish curl",
        rsi_state="Bullish divergence",
        bollinger_state="Compressed",
        relative_strength_state="Improving vs BTC/ETH",
    )
    score = apply_risk_adjusted_opportunity(
        score,
        volume,
        structure,
        liquidity,
        cap,
        targets,
        price=1.04,
        bottom=bottom,
    )
    assert score.components["StructuralOpportunityScore"] >= 60.0
    score.components["RiskAdjustedOpportunityScore"] = 72.0
    quote = LiveQuote(
        "XYZ-USD", 1.04, 1.039, 1.041, None, 2.0, now, now, now
    )
    candidate = Candidate(
        "XYZ-USD", "XYZ", SetupStatus.EARLY, quote, volume, structure,
        liquidity, cap, targets, score, bottom=bottom
    )

    assert qualification_blockers(candidate, settings, quote_age_seconds=1.0) == []
    active = SimpleNamespace(
        status=BottomStage.BOTTOM_FORMING.value,
        invalidation_price=0.97,
        trigger_price=1.08,
        component_scores={"RiskAdjustedOpportunityScore": 71.0, "BottomScore": 72.0, "BreakoutScore": 40.0},
        catalyst_id=None,
        raw_candidate={"bottom": {"stage": BottomStage.BOTTOM_FORMING.value}},
        detection_time=now - timedelta(minutes=30),
    )
    decision = AlertDeduplicator(settings).decide(candidate, active, quote_age_seconds=1.0)

    assert decision.should_send
    assert decision.alert_type == "status_transition"
