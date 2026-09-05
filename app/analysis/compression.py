from __future__ import annotations

from datetime import datetime, timedelta

from app.formatting import clamp
from app.types import CandlePoint


def range_pct(candles: list[CandlePoint]) -> float | None:
    if not candles:
        return None
    high = max(c.high for c in candles)
    low = min(c.low for c in candles)
    close = candles[-1].close
    if close <= 0:
        return None
    return (high - low) / close * 100.0


def compression_score(candles: list[CandlePoint], now: datetime) -> float:
    recent_1h = within(candles, now, timedelta(hours=1))
    recent_4h = within(candles, now, timedelta(hours=4))
    past_7d = within(candles, now, timedelta(days=7))
    r1 = range_pct(recent_1h)
    r4 = range_pct(recent_4h)
    r7 = range_pct(past_7d)
    if r1 is None or r4 is None or r7 is None or r7 <= 0:
        return 0.0
    local_tightness = 100.0 - min(100.0, r4 * 14.0)
    relative_tightness = 100.0 * max(0.0, 1.0 - (r4 / r7))
    immediate_tightness = 100.0 - min(100.0, r1 * 25.0)
    return clamp(local_tightness * 0.35 + relative_tightness * 0.45 + immediate_tightness * 0.20)


def within(candles: list[CandlePoint], now: datetime, window: timedelta) -> list[CandlePoint]:
    start = now - window
    return [c for c in candles if start <= c.start < now]
