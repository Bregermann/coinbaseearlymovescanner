from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.timeutils import parse_coinbase_time, utc_now
from app.types import CandlePoint


def minute_floor(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    return dt.replace(second=0, microsecond=0)


@dataclass(slots=True)
class TradeEvent:
    product_id: str
    price: float
    size: float
    side: str | None
    trade_time: datetime
    trade_id: str | None = None

    @property
    def quote_volume(self) -> float:
        return self.price * self.size


class TradeCandleBuilder:
    def __init__(self) -> None:
        self._current: dict[str, CandlePoint] = {}

    def ingest(self, trade: TradeEvent) -> CandlePoint | None:
        bucket = minute_floor(trade.trade_time)
        current = self._current.get(trade.product_id)
        closed: CandlePoint | None = None
        if current is not None and current.start != bucket:
            closed = current
            current = None
        if current is None:
            current = CandlePoint(
                product_id=trade.product_id,
                start=bucket,
                granularity_seconds=60,
                open=trade.price,
                high=trade.price,
                low=trade.price,
                close=trade.price,
                volume=trade.size,
                quote_volume=trade.quote_volume,
            )
            self._current[trade.product_id] = current
        else:
            current.high = max(current.high, trade.price)
            current.low = min(current.low, trade.price)
            current.close = trade.price
            current.volume += trade.size
            current.quote_volume += trade.quote_volume
        return closed

    def open_candles(self) -> list[CandlePoint]:
        return list(self._current.values())


def parse_trade(row: dict[str, Any]) -> TradeEvent | None:
    product_id = str(row.get("product_id") or "").upper()
    price = _to_float(row.get("price"))
    size = _to_float(row.get("size"))
    trade_time = parse_coinbase_time(row.get("time")) or utc_now()
    if not product_id or price is None or size is None:
        return None
    return TradeEvent(
        product_id=product_id,
        price=price,
        size=size,
        side=row.get("side"),
        trade_time=trade_time,
        trade_id=str(row.get("trade_id")) if row.get("trade_id") is not None else None,
    )


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
