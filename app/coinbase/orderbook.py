from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.analysis.liquidity import summarize_depth
from app.timeutils import parse_coinbase_time, utc_now


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class InMemoryBook:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    exchange_time: datetime | None = None

    def apply_update(self, side: str, price: float, size: float) -> None:
        target = self.bids if side in {"bid", "bids", "BUY"} else self.asks
        if size <= 0:
            target.pop(price, None)
        else:
            target[price] = size

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def summarize(self) -> dict[str, Any]:
        return summarize_depth(self.bids, self.asks, self.exchange_time)


class OrderBookManager:
    def __init__(self) -> None:
        self.books: dict[str, InMemoryBook] = defaultdict(InMemoryBook)

    def apply_event(self, event: dict[str, Any], fallback_time: str | None = None) -> dict[str, Any] | None:
        product_id = str(event.get("product_id") or "").upper()
        if not product_id:
            return None
        book = self.books[product_id]
        event_time = parse_coinbase_time(event.get("event_time") or fallback_time)
        if event_time:
            book.exchange_time = event_time
        if event.get("type") == "snapshot":
            book.bids.clear()
            book.asks.clear()
        for update in event.get("updates", []) or []:
            side = str(update.get("side") or update.get("type") or "").lower()
            if side in {"offer", "ask", "asks", "sell"}:
                side = "ask"
            elif side in {"bid", "bids", "buy"}:
                side = "bid"
            else:
                continue
            price = _to_float(update.get("price_level") or update.get("price"))
            size = _to_float(update.get("new_quantity") or update.get("size") or update.get("quantity"))
            if price is None or size is None:
                continue
            book.apply_update(side, price, size)
        summary = book.summarize()
        summary["captured_at"] = utc_now()
        summary["product_id"] = product_id
        return summary

    def get_best(self, product_id: str) -> tuple[float | None, float | None]:
        book = self.books.get(product_id.upper())
        if not book:
            return None, None
        return book.best_bid(), book.best_ask()
