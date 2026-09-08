from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import floor, log10
from statistics import median

from app.analysis.bottoms import ema_series, find_swings, resample_candles, true_ranges
from app.formatting import clamp
from app.types import BottomMetrics, BottomStage, CandlePoint, FibMetrics, StructureMetrics

RETRACEMENT_RATIOS = (0.236, 0.382, 0.500, 0.618, 0.786)
EXTENSION_RATIOS = (1.000, 1.272, 1.414, 1.618, 2.000, 2.618)
TIMEFRAMES = (
    (900, "15m", timedelta(days=2), 32, 5.0),
    (3600, "1h", timedelta(days=45), 30, 8.0),
    (14400, "4h", timedelta(days=120), 24, 12.0),
    (86400, "1d", timedelta(days=365), 15, 18.0),
)


@dataclass(slots=True)
class SwingCandidate:
    bars: list[CandlePoint]
    timeframe: str
    low_index: int
    high_index: int
    low: float
    high: float
    confidence: float
    volume_expansion: float
    atr_multiple: float


def analyze_fibonacci(
    candles: list[CandlePoint],
    current_price: float,
    now: datetime | None = None,
    *,
    structure: StructureMetrics | None = None,
    bottom: BottomMetrics | None = None,
    minimum_confidence: float = 62.0,
    probability_min_samples: int = 30,
) -> FibMetrics:
    if not candles or current_price <= 0:
        return empty_fib_metrics()
    candles = sorted(candles, key=lambda item: item.start)
    now = now or datetime.now(timezone.utc)
    swings: list[SwingCandidate] = []
    for seconds, label, lookback, minimum_bars, minimum_move in TIMEFRAMES:
        bars = resample_candles(candles, seconds, now, lookback)
        if len(bars) < minimum_bars:
            continue
        swing = select_active_impulse(
            bars,
            label,
            current_price,
            minimum_move_pct=minimum_move,
            structure=structure,
            bottom=bottom,
        )
        if swing is not None:
            swings.append(swing)
    if not swings:
        return empty_fib_metrics()

    swing = max(swings, key=lambda item: item.confidence)
    if swing.confidence < minimum_confidence:
        result = empty_fib_metrics()
        result.swing_confidence = round(swing.confidence, 2)
        result.timeframe = swing.timeframe
        result.reasons = ["The best detected impulse did not clear the configured swing-confidence floor."]
        return result

    span = swing.high - swing.low
    retracements = {
        ratio_label(value): round(swing.high - span * value, 12)
        for value in RETRACEMENT_RATIOS
    }
    extensions = {
        ratio_label(value): round(swing.low + span * value, 12)
        for value in EXTENSION_RATIOS
    }
    current_retracement = (swing.high - current_price) / span if span > 0 else None
    nearest_support, nearest_resistance = surrounding_levels(
        current_price,
        list(retracements.values()) + list(extensions.values()),
    )
    signal, tags = classify_fib_state(
        swing.bars,
        current_price,
        swing.high,
        retracements,
        extensions,
        current_retracement,
        bottom,
    )
    confluence, confluence_reasons = fib_confluence_score(
        swing,
        current_price,
        retracements,
        structure,
        bottom,
    )
    probabilities, probability_confidence = historical_extension_probabilities(
        swing.bars,
        minimum_samples=probability_min_samples,
    )
    if bottom and bottom.stage in {BottomStage.BOTTOM_FORMING, BottomStage.PRE_BREAKOUT}:
        if current_retracement is not None and 0.56 <= current_retracement <= 0.82:
            tags.append("FIB_BASE_BUILDING")
        if current_retracement is not None and 0.60 <= current_retracement <= 0.67:
            tags.append("GOLDEN_POCKET_ACCUMULATION")
    if signal in {"0.500 RECLAIM", "0.618 RECLAIM", "0.618 SUPPORT CONFIRMED"}:
        tags.append("FIB_RECLAIM")
    if bottom and bottom.stage == BottomStage.PRE_BREAKOUT and confluence >= 70.0:
        tags.append("FIB_BREAKOUT_READY")

    reasons = [
        f"{swing.timeframe} impulse spans {(swing.high - swing.low) / swing.low * 100.0:.1f}% and {swing.atr_multiple:.1f} ATR.",
        *confluence_reasons,
    ]
    return FibMetrics(
        reliable=True,
        anchor_low=round(swing.low, 12),
        anchor_high=round(swing.high, 12),
        anchor_timestamp_low=swing.bars[swing.low_index].start,
        anchor_timestamp_high=swing.bars[swing.high_index].start,
        timeframe=swing.timeframe,
        swing_confidence=round(swing.confidence, 2),
        retracements=retracements,
        extensions=extensions,
        current_retracement=round(current_retracement, 3) if current_retracement is not None else None,
        nearest_support=round(nearest_support, 12) if nearest_support is not None else None,
        nearest_resistance=round(nearest_resistance, 12) if nearest_resistance is not None else None,
        golden_pocket_low=round(swing.high - span * 0.650, 12),
        golden_pocket_high=round(swing.high - span * 0.618, 12),
        signal=signal,
        confluence_score=round(confluence, 2),
        tags=list(dict.fromkeys(tags)),
        reasons=reasons[:5],
        target_probabilities={"next_resistance": None, **probabilities},
        probability_confidence=probability_confidence,
    )


def empty_fib_metrics() -> FibMetrics:
    return FibMetrics(
        reliable=False,
        anchor_low=None,
        anchor_high=None,
        anchor_timestamp_low=None,
        anchor_timestamp_high=None,
        timeframe=None,
        swing_confidence=0.0,
    )


def select_active_impulse(
    bars: list[CandlePoint],
    timeframe: str,
    current_price: float,
    *,
    minimum_move_pct: float,
    structure: StructureMetrics | None,
    bottom: BottomMetrics | None,
) -> SwingCandidate | None:
    radius = 3 if len(bars) >= 80 else 2
    lows = find_swings(bars, highs=False, radius=radius)
    highs = find_swings(bars, highs=True, radius=radius)
    if not lows or not highs:
        return None
    true_range = median(true_ranges(bars[-80:])) if len(bars) >= 3 else 0.0
    candidates: list[SwingCandidate] = []
    max_separation = {"15m": 160, "1h": 240, "4h": 180, "1d": 180}.get(timeframe, 180)
    for high_index, high in highs[-14:]:
        prior_lows = [row for row in lows if 3 <= high_index - row[0] <= max_separation]
        if not prior_lows:
            continue
        low_index, low = min(prior_lows[-16:], key=lambda row: row[1])
        if low <= 0 or high <= low:
            continue
        move_pct = (high - low) / low * 100.0
        atr_multiple = (high - low) / true_range if true_range > 0 else 0.0
        if move_pct < minimum_move_pct or atr_multiple < 3.5:
            continue
        if current_price < low * 0.90 or current_price > low + (high - low) * 2.15:
            continue
        volume_expansion = pivot_volume_expansion(bars, high_index)
        age = len(bars) - 1 - high_index
        age_limit = max(24, int(len(bars) * 0.72))
        if age > age_limit:
            continue
        magnitude = clamp((move_pct - minimum_move_pct) * 2.0 + 45.0)
        atr_score = clamp((atr_multiple - 3.0) * 11.0 + 45.0)
        volume_score = clamp((volume_expansion - 0.8) * 55.0 + 42.0)
        recency_score = clamp(100.0 - age / max(1, age_limit) * 55.0)
        structure_score = active_structure_alignment(low, high, current_price, structure, bottom)
        timeframe_bonus = {"15m": 0.0, "1h": 5.0, "4h": 7.0, "1d": 3.0}.get(timeframe, 0.0)
        confidence = clamp(
            magnitude * 0.24
            + atr_score * 0.20
            + volume_score * 0.15
            + recency_score * 0.16
            + structure_score * 0.25
            + timeframe_bonus
        )
        candidates.append(
            SwingCandidate(
                bars=bars,
                timeframe=timeframe,
                low_index=low_index,
                high_index=high_index,
                low=low,
                high=high,
                confidence=confidence,
                volume_expansion=volume_expansion,
                atr_multiple=atr_multiple,
            )
        )
    return max(candidates, key=lambda item: item.confidence, default=None)


def active_structure_alignment(
    low: float,
    high: float,
    price: float,
    structure: StructureMetrics | None,
    bottom: BottomMetrics | None,
) -> float:
    span = high - low
    retracement = (high - price) / span if span > 0 else -1.0
    score = 45.0
    if -0.08 <= retracement <= 0.82:
        score += 20.0
    if 0.35 <= retracement <= 0.80:
        score += 15.0
    levels = (
        structure.support_low if structure else None,
        structure.resistance if structure else None,
        bottom.support if bottom else None,
        bottom.resistance if bottom else None,
    )
    if any(
        level and any(abs(level - (high - span * ratio)) / level <= 0.02 for ratio in RETRACEMENT_RATIOS)
        for level in levels
    ):
        score += 15.0
    return clamp(score)


def pivot_volume_expansion(bars: list[CandlePoint], index: int) -> float:
    prior = [bar.quote_volume for bar in bars[max(0, index - 20):index] if bar.quote_volume > 0]
    if not prior:
        return 1.0
    pivot_window = bars[max(0, index - 1):min(len(bars), index + 2)]
    return max((bar.quote_volume for bar in pivot_window), default=0.0) / median(prior)


def classify_fib_state(
    bars: list[CandlePoint],
    price: float,
    anchor_high: float,
    retracements: dict[str, float],
    extensions: dict[str, float],
    current_retracement: float | None,
    bottom: BottomMetrics | None,
) -> tuple[str, list[str]]:
    if not bars:
        return "N/A", []
    previous = bars[-2].close if len(bars) >= 2 else bars[-1].open
    latest = bars[-1]
    tolerance = max(0.006, median_true_range_pct(bars[-30:]) * 0.75 / 100.0)
    level_500 = retracements["0.500"]
    level_618 = retracements["0.618"]
    level_786 = retracements["0.786"]
    if previous <= anchor_high < price:
        return "1.000 BREAKOUT", ["FIB_RECLAIM"]
    for label in ("1.272", "1.414", "1.618"):
        level = extensions[label]
        if abs(price - level) / price <= tolerance:
            return f"{label} EXTENSION TEST" if label != "1.618" else "1.618 EXTENSION TARGET", []
    if previous < level_618 <= price:
        return "0.618 RECLAIM", ["FIB_RECLAIM"]
    if latest.low <= level_618 * (1 + tolerance) and latest.close >= level_618:
        return "0.618 SUPPORT CONFIRMED", ["GOLDEN_POCKET_ACCUMULATION"]
    if current_retracement is not None and 0.60 <= current_retracement <= 0.67:
        return "GOLDEN POCKET TEST", ["GOLDEN_POCKET_ACCUMULATION"]
    if previous < level_500 <= price:
        return "0.500 RECLAIM", ["FIB_RECLAIM"]
    if current_retracement is not None and 0.35 <= current_retracement <= 0.42:
        return "0.382 BOUNCE", []
    if current_retracement is not None and 0.74 <= current_retracement <= 0.82:
        return "0.786 DEEP RETRACEMENT", ["FIB_BASE_BUILDING"]
    if price < level_786:
        return "LOSS OF 0.786", []
    if price < level_618 and bottom and bottom.stage != BottomStage.NONE:
        return "LOSS OF 0.618", ["FIB_BASE_BUILDING"]
    return "ACTIVE IMPULSE", []


def fib_confluence_score(
    swing: SwingCandidate,
    price: float,
    retracements: dict[str, float],
    structure: StructureMetrics | None,
    bottom: BottomMetrics | None,
) -> tuple[float, list[str]]:
    bars = swing.bars
    reference_levels = [retracements[key] for key in ("0.382", "0.500", "0.618", "0.786")]
    active_level = min(reference_levels, key=lambda value: abs(value - price))
    tolerance = min(
        active_level * 0.02,
        max(active_level * 0.006, median(true_ranges(bars[-30:])) * 0.35),
    )
    score = 16.0 + swing.confidence * 0.22
    reasons: list[str] = []

    structural_levels = [
            structure.support_low if structure else None,
            structure.support_high if structure else None,
            structure.resistance if structure else None,
            structure.breakout_trigger if structure else None,
            bottom.support if bottom else None,
            bottom.resistance if bottom else None,
    ]
    if any(level and abs(level - active_level) <= tolerance for level in structural_levels):
        score += 18.0
        reasons.append("Fib level overlaps independently detected horizontal/base structure.")

    closes = [bar.close for bar in bars]
    moving_averages = [ema_series(closes, period)[-1] for period in (20, 50) if len(closes) >= period]
    if any(abs(value - active_level) <= tolerance for value in moving_averages):
        score += 12.0
        reasons.append("Fib level overlaps a 20/50-period EMA.")
    recent = bars[-96:]
    total_volume = sum(bar.quote_volume for bar in recent)
    if total_volume > 0:
        vwap = sum(((bar.high + bar.low + bar.close) / 3.0) * bar.quote_volume for bar in recent) / total_volume
        if abs(vwap - active_level) <= tolerance:
            score += 12.0
            reasons.append("Fib level overlaps recent VWAP.")
    pivots = [value for _, value in find_swings(bars[-160:], highs=False)] + [
        value for _, value in find_swings(bars[-160:], highs=True)
    ]
    if any(abs(value - active_level) <= tolerance for value in pivots):
        score += 13.0
        reasons.append("Fib level overlaps a local pivot cluster.")
    shelf = volume_shelf(bars[-160:], active_level, tolerance)
    if shelf >= 1.35:
        score += 13.0
        reasons.append("Fib level overlaps a high-volume price shelf.")
    if near_round_number(active_level, tolerance):
        score += 5.0
        reasons.append("Fib level is near a round-number reference.")
    return clamp(score), reasons


def volume_shelf(bars: list[CandlePoint], level: float, tolerance: float) -> float:
    if not bars:
        return 0.0
    near = [bar.quote_volume for bar in bars if abs(((bar.high + bar.low + bar.close) / 3.0) - level) <= tolerance]
    all_volume = [bar.quote_volume for bar in bars if bar.quote_volume > 0]
    if len(near) < 2 or not all_volume or median(all_volume) <= 0:
        return 0.0
    return median(near) / median(all_volume)


def near_round_number(level: float, tolerance: float) -> bool:
    if level <= 0:
        return False
    magnitude = 10 ** floor(log10(level))
    step = magnitude * (0.05 if level >= 1 else 0.5)
    if step <= 0:
        return False
    nearest = round(level / step) * step
    return abs(level - nearest) <= tolerance


def surrounding_levels(price: float, levels: list[float]) -> tuple[float | None, float | None]:
    supports = [value for value in levels if value <= price]
    resistances = [value for value in levels if value > price]
    return (max(supports) if supports else None, min(resistances) if resistances else None)


def median_true_range_pct(bars: list[CandlePoint]) -> float:
    values = true_ranges(bars)
    close = median([bar.close for bar in bars]) if bars else 0.0
    return median(values) / close * 100.0 if values and close > 0 else 0.0


def historical_extension_probabilities(
    bars: list[CandlePoint],
    *,
    minimum_samples: int = 30,
) -> tuple[dict[str, float | None], str]:
    labels = ("1.272", "1.414", "1.618", "2.000")
    empty = {label: None for label in labels}
    if len(bars) < max(120, minimum_samples * 6):
        return empty, "insufficient historical sample"
    lows = find_swings(bars, highs=False, radius=2)
    highs = find_swings(bars, highs=True, radius=2)
    ranges = true_ranges(bars)
    typical_range = median(ranges) if ranges else 0.0
    outcomes: list[float] = []
    for high_index, high in highs:
        if high_index >= len(bars) - 16:
            continue
        prior_lows = [row for row in lows if 4 <= high_index - row[0] <= 120]
        if not prior_lows:
            continue
        low_index, low = min(prior_lows[-12:], key=lambda row: row[1])
        span = high - low
        if low <= 0 or span / low < 0.06 or (typical_range > 0 and span / typical_range < 3.5):
            continue
        pullback_end = min(len(bars), high_index + 49)
        pullback_level = high - span * 0.382
        invalidation = low * 0.985
        retracement_index = next(
            (
                index for index in range(high_index + 1, pullback_end)
                if bars[index].low <= pullback_level and bars[index].low > invalidation
            ),
            None,
        )
        if retracement_index is None:
            continue
        reclaim_level = high - span * 0.618
        entry_index = next(
            (
                index for index in range(retracement_index, min(len(bars), retracement_index + 25))
                if bars[index].close >= reclaim_level
            ),
            None,
        )
        if entry_index is None or entry_index >= len(bars) - 12:
            continue
        future = bars[entry_index + 1:min(len(bars), entry_index + 97)]
        observed_high = high
        for bar in future:
            if bar.low <= invalidation:
                break
            observed_high = max(observed_high, bar.high)
        outcomes.append((observed_high - low) / span)
    if len(outcomes) < minimum_samples:
        return empty, f"insufficient historical sample (n={len(outcomes)})"
    probabilities = {
        label: round(sum(outcome >= float(label) for outcome in outcomes) / len(outcomes) * 100.0, 1)
        for label in labels
    }
    confidence = "HIGH" if len(outcomes) >= minimum_samples * 3 else "MEDIUM"
    return probabilities, f"{confidence} empirical confidence (n={len(outcomes)})"


def ratio_label(value: float) -> str:
    return f"{value:.3f}"
