from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.types import CandlePoint, VolumeMetrics

WINDOWS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "12h": 43200,
    "24h": 86400,
}


def calculate_volume_metrics(candles: list[CandlePoint], now: datetime | None = None) -> VolumeMetrics:
    if not candles:
        return VolumeMetrics({}, {}, 0.0, 0.0, 0.0)
    candles = sorted(candles, key=lambda item: item.start)
    now = now or max(c.start + timedelta(seconds=c.granularity_seconds) for c in candles)
    volume_quote = {label: sum_quote_volume(candles, now, seconds) for label, seconds in WINDOWS.items()}
    ratios: dict[str, float] = {}

    for label, seconds in WINDOWS.items():
        current = volume_quote[label]
        previous = sum_quote_volume(candles, now - timedelta(seconds=seconds), seconds)
        ratios[f"{label}_vs_previous"] = safe_ratio(current, previous)

    ratios["5m_vs_baseline"] = safe_ratio(volume_quote["5m"], average_bucket_volume(candles, now, 300, timedelta(hours=24)))
    ratios["15m_vs_baseline"] = safe_ratio(volume_quote["15m"], average_bucket_volume(candles, now, 900, timedelta(hours=24)))
    ratios["1h_vs_7d"] = safe_ratio(volume_quote["1h"], average_bucket_volume(candles, now, 3600, timedelta(days=7)))
    ratios["1h_vs_30d"] = safe_ratio(volume_quote["1h"], average_bucket_volume(candles, now, 3600, timedelta(days=30)))
    ratios["4h_vs_7d"] = safe_ratio(volume_quote["4h"], average_bucket_volume(candles, now, 14400, timedelta(days=7)))
    ratios["24h_vs_7d"] = safe_ratio(volume_quote["24h"], average_daily_volume(candles, now, timedelta(days=7)))
    ratios["24h_vs_30d"] = safe_ratio(volume_quote["24h"], average_daily_volume(candles, now, timedelta(days=30)))

    price_5m = abs(price_change_pct(candles, now, 300) or 0.0)
    price_1h = abs(price_change_pct(candles, now, 3600) or 0.0)
    price_acceleration = max(price_5m, price_1h / 2.0)
    volume_acceleration = weighted_volume_acceleration(ratios)
    volume_vs_price = volume_acceleration / max(1.0, 1.0 + price_acceleration / 4.0)
    return VolumeMetrics(
        volume_quote=volume_quote,
        ratios=ratios,
        volume_acceleration=volume_acceleration,
        price_acceleration=price_acceleration,
        volume_vs_price_acceleration=volume_vs_price,
    )


def sum_quote_volume(candles: list[CandlePoint], end: datetime, seconds: int) -> float:
    start = end - timedelta(seconds=seconds)
    return sum(c.quote_volume for c in candles if start <= c.start < end and c.granularity_seconds <= seconds)


def average_bucket_volume(candles: list[CandlePoint], now: datetime, bucket_seconds: int, lookback: timedelta) -> float:
    start = now - lookback
    current_start = now - timedelta(seconds=bucket_seconds)
    buckets: dict[int, float] = {}
    for candle in candles:
        if candle.start < start or candle.start >= current_start or candle.granularity_seconds > bucket_seconds:
            continue
        bucket_id = int((candle.start - start).total_seconds() // bucket_seconds)
        buckets[bucket_id] = buckets.get(bucket_id, 0.0) + candle.quote_volume
    values = [value for value in buckets.values() if value > 0]
    return sum(values) / len(values) if values else 0.0


def average_daily_volume(candles: list[CandlePoint], now: datetime, lookback: timedelta) -> float:
    start = now - lookback
    current_start = now - timedelta(days=1)
    buckets: dict[int, float] = {}
    for candle in candles:
        if candle.start < start or candle.start >= current_start:
            continue
        bucket_id = int((candle.start - start).total_seconds() // 86400)
        buckets[bucket_id] = buckets.get(bucket_id, 0.0) + candle.quote_volume
    values = [value for value in buckets.values() if value > 0]
    return sum(values) / len(values) if values else 0.0


def price_change_pct(candles: list[CandlePoint], now: datetime, seconds: int) -> float | None:
    start = now - timedelta(seconds=seconds)
    eligible = [c for c in candles if start <= c.start < now]
    if not eligible:
        return None
    first = eligible[0].open
    last = eligible[-1].close
    if first <= 0:
        return None
    return (last - first) / first * 100.0


def weighted_volume_acceleration(ratios: dict[str, float]) -> float:
    weights = {
        "5m_vs_baseline": 0.26,
        "15m_vs_baseline": 0.22,
        "1h_vs_7d": 0.18,
        "1h_vs_30d": 0.12,
        "4h_vs_7d": 0.12,
        "24h_vs_7d": 0.05,
        "24h_vs_30d": 0.05,
    }
    score = 0.0
    for key, weight in weights.items():
        score += min(ratios.get(key, 0.0), 12.0) * weight
    return score


def safe_ratio(numerator: float, denominator: float) -> float:
    if numerator <= 0:
        return 0.0
    if denominator <= 0:
        return 12.0
    value = numerator / denominator
    if not math.isfinite(value):
        return 0.0
    return min(value, 99.0)
