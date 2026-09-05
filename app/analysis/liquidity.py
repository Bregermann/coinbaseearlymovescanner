from __future__ import annotations

from datetime import datetime
from typing import Any

from app.formatting import clamp
from app.types import LiquidityMetrics

DEPTH_WINDOWS = (0.25, 0.5, 1.0, 2.0, 5.0)


def summarize_depth(
    bids: dict[float, float],
    asks: dict[float, float],
    exchange_time: datetime | None = None,
) -> dict[str, Any]:
    best_bid = max(bids) if bids else None
    best_ask = min(asks) if asks else None
    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
        return {
            "exchange_time": exchange_time,
            "spread_pct": None,
            "bid_depth_usd": {str(w).rstrip("0").rstrip("."): 0.0 for w in DEPTH_WINDOWS},
            "ask_depth_usd": {str(w).rstrip("0").rstrip("."): 0.0 for w in DEPTH_WINDOWS},
            "imbalance": None,
            "liquidity_vacuum_score": 0.0,
            "order_book_score": 0.0,
        }
    mid = (best_bid + best_ask) / 2.0
    spread_pct = (best_ask - best_bid) / mid * 100.0
    bid_depth = {label(window): depth_within(bids, mid, window, "bid") for window in DEPTH_WINDOWS}
    ask_depth = {label(window): depth_within(asks, mid, window, "ask") for window in DEPTH_WINDOWS}
    total_bid_1 = bid_depth["1"]
    total_ask_1 = ask_depth["1"]
    imbalance = (total_bid_1 - total_ask_1) / (total_bid_1 + total_ask_1) if total_bid_1 + total_ask_1 > 0 else None
    vacuum = liquidity_vacuum_score(ask_depth)
    book_score = order_book_score(spread_pct, imbalance, vacuum)
    return {
        "exchange_time": exchange_time,
        "spread_pct": spread_pct,
        "bid_depth_usd": bid_depth,
        "ask_depth_usd": ask_depth,
        "imbalance": imbalance,
        "liquidity_vacuum_score": vacuum,
        "order_book_score": book_score,
    }


def metrics_from_summary(summary: dict[str, Any] | None) -> LiquidityMetrics:
    if not summary:
        return LiquidityMetrics(None, {}, {}, None, 0.0, 0.0)
    return LiquidityMetrics(
        spread_pct=summary.get("spread_pct"),
        bid_depth_usd=summary.get("bid_depth_usd", {}) or {},
        ask_depth_usd=summary.get("ask_depth_usd", {}) or {},
        imbalance=summary.get("imbalance"),
        liquidity_vacuum_score=float(summary.get("liquidity_vacuum_score") or 0.0),
        order_book_score=float(summary.get("order_book_score") or 0.0),
    )


def metrics_from_model(row: Any | None) -> LiquidityMetrics:
    if row is None:
        return LiquidityMetrics(None, {}, {}, None, 0.0, 0.0)
    bid = {
        "0.25": row.bid_depth_0_25_pct,
        "0.5": row.bid_depth_0_5_pct,
        "1": row.bid_depth_1_pct,
        "2": row.bid_depth_2_pct,
        "5": row.bid_depth_5_pct,
    }
    ask = {
        "0.25": row.ask_depth_0_25_pct,
        "0.5": row.ask_depth_0_5_pct,
        "1": row.ask_depth_1_pct,
        "2": row.ask_depth_2_pct,
        "5": row.ask_depth_5_pct,
    }
    return LiquidityMetrics(row.spread_pct, bid, ask, row.imbalance, row.liquidity_vacuum_score, row.raw_summary.get("order_book_score", 0.0) if row.raw_summary else 0.0)


def depth_within(levels: dict[float, float], mid: float, window_pct: float, side: str) -> float:
    if side == "bid":
        floor = mid * (1.0 - window_pct / 100.0)
        return sum(price * size for price, size in levels.items() if floor <= price <= mid)
    ceiling = mid * (1.0 + window_pct / 100.0)
    return sum(price * size for price, size in levels.items() if mid <= price <= ceiling)


def liquidity_vacuum_score(ask_depth: dict[str, float]) -> float:
    ask_1 = ask_depth.get("1", 0.0)
    ask_5 = ask_depth.get("5", 0.0)
    if ask_5 <= 0:
        return 0.0
    near_ratio = ask_1 / ask_5
    return clamp((1.0 - near_ratio) * 100.0)


def order_book_score(spread_pct: float | None, imbalance: float | None, vacuum: float) -> float:
    if spread_pct is None:
        return 0.0
    spread_score = clamp(100.0 - spread_pct * 250.0)
    imbalance_score = clamp(((imbalance or 0.0) + 1.0) * 50.0)
    return clamp(spread_score * 0.35 + imbalance_score * 0.35 + vacuum * 0.30)


def label(value: float) -> str:
    return str(value).rstrip("0").rstrip(".")
