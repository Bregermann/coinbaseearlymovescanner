from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median

from app.analysis.compression import compression_score, within
from app.formatting import clamp
from app.types import CandlePoint, SetupStatus, StructureMetrics

HIGH_WINDOWS = {
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "24h": timedelta(hours=24),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
}


def analyze_structure(candles: list[CandlePoint], current_price: float, now: datetime | None = None) -> StructureMetrics:
    candles = sorted(candles, key=lambda item: item.start)
    now = now or (candles[-1].start + timedelta(seconds=candles[-1].granularity_seconds) if candles else datetime.now(timezone.utc))
    if not candles or current_price <= 0:
        return StructureMetrics(
            status=SetupStatus.EARLY,
            base_low=None,
            base_high=None,
            support_low=None,
            support_high=None,
            resistance=None,
            breakout_trigger=None,
            confirmation=None,
            invalidation=None,
            distance_from_base_pct=None,
            distance_from_resistance_pct=None,
            highs={},
            compression_score=0.0,
            higher_low_score=0.0,
            base_proximity_score=0.0,
            resistance_proximity_score=0.0,
            extension_24h_pct=None,
        )

    highs = {label: _high(within(candles, now, window)) for label, window in HIGH_WINDOWS.items()}
    highs = {key: value for key, value in highs.items() if value is not None}
    last_24h = within(candles, now, timedelta(hours=24)) or candles[-min(len(candles), 24) :]
    last_4h = within(candles, now, timedelta(hours=4)) or last_24h
    base_low, base_high = detect_base(last_24h, current_price)
    support_low, support_high = detect_support(last_4h, last_24h)
    resistance = detect_resistance(candles, current_price, highs)
    breakout_trigger = resistance
    confirmation = detect_confirmation(current_price, resistance, highs)
    invalidation = support_low if support_low is not None else (base_low * 0.995 if base_low else None)
    extension_24h = price_extension(candles, now, current_price, timedelta(hours=24))
    base_distance = ((current_price - base_low) / current_price * 100.0) if base_low else None
    resistance_distance = ((resistance - current_price) / current_price * 100.0) if resistance else None
    comp = compression_score(candles, now)
    hl = higher_low_score(last_4h)
    base_score = base_proximity_score(base_low, base_high, current_price)
    resistance_score = resistance_proximity_score(resistance_distance)
    status = classify_status(current_price, breakout_trigger, extension_24h, comp, base_score)

    return StructureMetrics(
        status=status,
        base_low=base_low,
        base_high=base_high,
        support_low=support_low,
        support_high=support_high,
        resistance=resistance,
        breakout_trigger=breakout_trigger,
        confirmation=confirmation,
        invalidation=invalidation,
        distance_from_base_pct=base_distance,
        distance_from_resistance_pct=resistance_distance,
        highs=highs,
        compression_score=comp,
        higher_low_score=hl,
        base_proximity_score=base_score,
        resistance_proximity_score=resistance_score,
        extension_24h_pct=extension_24h,
    )


def detect_base(candles: list[CandlePoint], price: float) -> tuple[float | None, float | None]:
    if not candles:
        return None, None
    lows = sorted(c.low for c in candles)
    highs = sorted(c.high for c in candles)
    low = percentile(lows, 0.10)
    high = percentile(highs, 0.35)
    if high <= low:
        high = low + (max(highs) - low) * 0.35
    if price < low:
        low = min(price, low)
    return low, high


def detect_support(last_4h: list[CandlePoint], last_24h: list[CandlePoint]) -> tuple[float | None, float | None]:
    source = last_4h if len(last_4h) >= 3 else last_24h
    if not source:
        return None, None
    lows = sorted(c.low for c in source)
    support_low = percentile(lows, 0.10)
    support_high = percentile(lows, 0.30)
    return support_low, support_high


def detect_resistance(candles: list[CandlePoint], price: float, highs: dict[str, float]) -> float | None:
    candidates = [value for value in highs.values() if value > price * 1.001]
    recent_wicks = [c.high for c in candles[-200:] if c.high > price * 1.001]
    candidates.extend(recent_wicks)
    if candidates:
        return min(candidates)
    if highs:
        return max(highs.values())
    return None


def detect_confirmation(price: float, resistance: float | None, highs: dict[str, float]) -> float | None:
    if resistance is None:
        return None
    higher = sorted(value for value in highs.values() if value > resistance * 1.002)
    if higher:
        return higher[0]
    recent_range = max(abs(resistance - price), price * 0.003)
    return resistance + recent_range


def price_extension(candles: list[CandlePoint], now: datetime, price: float, window: timedelta) -> float | None:
    source = within(candles, now, window)
    if not source:
        return None
    first = source[0].open
    if first <= 0:
        return None
    return (price - first) / first * 100.0


def classify_status(
    price: float,
    breakout_trigger: float | None,
    extension_24h: float | None,
    comp: float,
    base_score: float,
) -> SetupStatus:
    if extension_24h is not None and extension_24h >= 20.0 and not (comp >= 65.0 and base_score >= 55.0):
        return SetupStatus.ALREADY_EXTENDED
    if breakout_trigger is not None and price >= breakout_trigger:
        return SetupStatus.CONFIRMING
    return SetupStatus.EARLY


def higher_low_score(candles: list[CandlePoint]) -> float:
    if len(candles) < 9:
        return 0.0
    segments = split_segments(candles, 3)
    lows = [min(c.low for c in segment) for segment in segments if segment]
    if len(lows) < 3:
        return 0.0
    slope_score = 100.0 if lows[2] > lows[1] > lows[0] else 55.0 if lows[2] > lows[0] else 0.0
    smoothness = 100.0 - min(100.0, abs((lows[1] - median(lows)) / median(lows)) * 5000.0) if median(lows) else 0.0
    return clamp(slope_score * 0.75 + smoothness * 0.25)


def base_proximity_score(base_low: float | None, base_high: float | None, price: float) -> float:
    if base_low is None or base_high is None or price <= 0:
        return 0.0
    if base_low <= price <= base_high:
        return 100.0
    distance = max(0.0, price - base_high) / price * 100.0
    return clamp(100.0 - distance * 15.0)


def resistance_proximity_score(distance_pct: float | None) -> float:
    if distance_pct is None:
        return 0.0
    if distance_pct < 0:
        return 70.0
    if distance_pct <= 0.75:
        return 100.0
    if distance_pct <= 4.0:
        return 85.0 - distance_pct * 8.0
    return clamp(45.0 - (distance_pct - 4.0) * 4.0)


def split_segments(candles: list[CandlePoint], count: int) -> list[list[CandlePoint]]:
    size = max(1, len(candles) // count)
    segments = [candles[i * size : (i + 1) * size] for i in range(count - 1)]
    segments.append(candles[(count - 1) * size :])
    return segments


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    pos = max(0, min(len(values) - 1, int(round((len(values) - 1) * q))))
    return values[pos]


def _high(candles: list[CandlePoint]) -> float | None:
    return max((c.high for c in candles), default=None)
