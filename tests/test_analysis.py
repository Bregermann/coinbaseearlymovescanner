from datetime import datetime, timedelta, timezone

from app.analysis.liquidity import summarize_depth
from app.analysis.scoring import score_candidate
from app.analysis.structure import analyze_structure
from app.analysis.targets import generate_targets
from app.analysis.volume import calculate_volume_metrics
from app.types import CandlePoint, LiquidityMetrics, MarketCapMetrics


def candle(start, open_price=1.0, high=1.01, low=0.99, close=1.0, quote_volume=100.0):
    return CandlePoint(
        product_id="XYZ-USD",
        start=start,
        granularity_seconds=60,
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
