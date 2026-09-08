from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from app.analysis.bottoms import analyze_bottom_structure, resample_candles
from app.config import Settings
from app.types import BottomMetrics, BottomStage, CandlePoint


@dataclass(slots=True)
class BottomValidationResult:
    symbol: str
    product_id: str
    first_alert_timestamp: datetime | None
    first_alert_price: float | None
    breakout_timestamp: datetime | None
    breakout_price: float | None
    maximum_price_after_alert: float | None
    maximum_upside_after_alert_pct: float | None
    alert_stage: str | None
    bottom_score: float | None
    breakout_score: float | None
    alerted_before_breakout: bool
    failure_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("first_alert_timestamp", "breakout_timestamp"):
            value = result[key]
            result[key] = value.isoformat() if isinstance(value, datetime) else None
        return result


def validate_bottom_history(
    symbol: str,
    product_id: str,
    candles: list[CandlePoint],
    *,
    benchmark_candles: dict[str, list[CandlePoint]],
    now: datetime,
    settings: Settings,
    breakout_lookback_days: int = 7,
) -> BottomValidationResult:
    bars = resample_candles(
        candles,
        900,
        now,
        timedelta(days=max(10, breakout_lookback_days + 3)),
    )
    breakout_index = find_reference_breakout(
        bars, now - timedelta(days=breakout_lookback_days)
    )
    if breakout_index is None:
        return BottomValidationResult(
            symbol=symbol,
            product_id=product_id,
            first_alert_timestamp=None,
            first_alert_price=None,
            breakout_timestamp=None,
            breakout_price=None,
            maximum_price_after_alert=None,
            maximum_upside_after_alert_pct=None,
            alert_stage=None,
            bottom_score=None,
            breakout_score=None,
            alerted_before_breakout=False,
            failure_reason="No >=5% price expansion with confirming volume was found in the replay window.",
        )

    breakout_bar = bars[breakout_index]
    actionable: list[tuple[int, datetime, float, BottomMetrics]] = []
    best: tuple[float, BottomMetrics] | None = None
    replay_start = max(40, breakout_index - 96 * breakout_lookback_days)
    replay_end = min(len(bars) - 1, breakout_index + 1)
    for index in range(replay_start, replay_end + 1):
        evaluation_time = bars[index].start + timedelta(minutes=15)
        price = bars[index].close
        metrics = analyze_bottom_structure(
            candles,
            price,
            evaluation_time,
            benchmark_candles=benchmark_candles,
        )
        quality = metrics.bottom_score + metrics.breakout_score * 0.35
        if best is None or quality > best[0]:
            best = (quality, metrics)
        if structurally_actionable(metrics, settings):
            actionable.append((index, evaluation_time, price, metrics))

    surviving = [
        row for row in actionable
        if not invalidated_before_breakout(row[0], breakout_index, bars, row[3])
    ]
    first_alert = surviving[0] if surviving else None
    if first_alert is None:
        metrics = best[1] if best else None
        return BottomValidationResult(
            symbol=symbol,
            product_id=product_id,
            first_alert_timestamp=None,
            first_alert_price=None,
            breakout_timestamp=breakout_bar.start,
            breakout_price=breakout_bar.open,
            maximum_price_after_alert=None,
            maximum_upside_after_alert_pct=None,
            alert_stage=None,
            bottom_score=metrics.bottom_score if metrics else None,
            breakout_score=metrics.breakout_score if metrics else None,
            alerted_before_breakout=False,
            failure_reason=failure_reason(metrics, settings),
        )

    _alert_index, alert_time, alert_price, metrics = first_alert
    horizon_end = breakout_bar.start + timedelta(hours=24)
    future = [
        bar for bar in bars
        if alert_time <= bar.start < horizon_end
    ]
    maximum_price = max((bar.high for bar in future), default=alert_price)
    upside = (maximum_price - alert_price) / alert_price * 100 if alert_price > 0 else None
    return BottomValidationResult(
        symbol=symbol,
        product_id=product_id,
        first_alert_timestamp=alert_time,
        first_alert_price=alert_price,
        breakout_timestamp=breakout_bar.start,
        breakout_price=breakout_bar.open,
        maximum_price_after_alert=maximum_price,
        maximum_upside_after_alert_pct=upside,
        alert_stage=metrics.stage.value,
        bottom_score=metrics.bottom_score,
        breakout_score=metrics.breakout_score,
        alerted_before_breakout=alert_time <= breakout_bar.start,
        failure_reason=None if alert_time <= breakout_bar.start + timedelta(minutes=15) else "Alert arrived after the reference breakout.",
    )


def invalidated_before_breakout(
    alert_index: int,
    breakout_index: int,
    bars: list[CandlePoint],
    metrics: BottomMetrics,
) -> bool:
    if metrics.invalidation is None:
        return False
    return any(
        bar.low <= metrics.invalidation
        for bar in bars[alert_index + 1:breakout_index]
    )


def find_reference_breakout(
    bars: list[CandlePoint],
    since: datetime,
) -> int | None:
    candidates: list[tuple[float, int]] = []
    for index in range(21, len(bars) - 3):
        bar = bars[index]
        if bar.start < since:
            continue
        previous_price = bars[index - 1].close
        if previous_price <= 0:
            continue
        forward_high = max(row.high for row in bars[index:index + 4])
        forward_gain = (forward_high - previous_price) / previous_price * 100
        baseline_values = [
            row.quote_volume for row in bars[index - 20:index]
            if row.quote_volume > 0
        ]
        baseline = median(baseline_values) if baseline_values else 0.0
        volume_ratio = bar.quote_volume / baseline if baseline > 0 else 0.0
        if forward_gain >= 5.0 and volume_ratio >= 1.15:
            ranking = forward_gain + min(volume_ratio, 10.0) * 1.5
            candidates.append((ranking, index))
    return max(candidates)[1] if candidates else None


def structurally_actionable(metrics: BottomMetrics, settings: Settings) -> bool:
    if metrics.stage == BottomStage.PRE_BREAKOUT:
        return (
            metrics.bottom_score >= settings.min_pre_breakout_score
            and metrics.distance_to_breakout_pct is not None
            and metrics.distance_to_breakout_pct <= settings.pre_breakout_max_distance_pct
        )
    if metrics.stage == BottomStage.BOTTOM_FORMING:
        return metrics.bottom_score >= settings.min_bottom_forming_score
    if metrics.stage == BottomStage.BREAKOUT_FIRING:
        return metrics.breakout_score >= settings.min_breakout_firing_score
    if metrics.stage == BottomStage.RETEST_HOLD:
        return metrics.bottom_score >= 62.0
    return False


def failure_reason(
    metrics: BottomMetrics | None,
    settings: Settings,
) -> str:
    if metrics is None:
        return "Insufficient candle history for bottom scoring."
    reasons: list[str] = []
    if metrics.bottom_score < settings.min_pre_breakout_score:
        reasons.append(
            f"bottom_score {metrics.bottom_score:.1f} < {settings.min_pre_breakout_score:.1f}"
        )
    if metrics.distance_to_breakout_pct is None:
        reasons.append("no defensible resistance boundary")
    elif metrics.distance_to_breakout_pct > settings.pre_breakout_max_distance_pct:
        reasons.append(
            f"distance_to_breakout {metrics.distance_to_breakout_pct:.1f}% > "
            f"{settings.pre_breakout_max_distance_pct:.1f}%"
        )
    if metrics.breakout_score < settings.min_breakout_firing_score:
        reasons.append(
            f"breakout_score {metrics.breakout_score:.1f} < "
            f"{settings.min_breakout_firing_score:.1f}"
        )
    weakest = sorted(metrics.components.items(), key=lambda item: item[1])[:3]
    reasons.append(
        "weakest components: "
        + ", ".join(f"{name}={value:.1f}" for name, value in weakest)
    )
    return "; ".join(reasons)
