from __future__ import annotations

from datetime import datetime, timedelta

from app.analysis.compression import range_pct, within
from app.formatting import clamp
from app.types import CandlePoint, LiveQuote, MarketCapMetrics


def market_cap_metrics_from_metadata(metadata: object | None, quote: LiveQuote) -> MarketCapMetrics:
    if metadata is None:
        volume_to_mcap = None
        return MarketCapMetrics(None, None, None, None, None, volume_to_mcap, None)
    circulating_supply = getattr(metadata, "circulating_supply", None)
    total_supply = getattr(metadata, "total_supply", None)
    max_supply = getattr(metadata, "max_supply", None)
    fdv = getattr(metadata, "fdv", None)
    supply_for_fdv = max_supply or total_supply
    live_fdv = quote.price * supply_for_fdv if supply_for_fdv else fdv
    circulating_market_cap = quote.price * circulating_supply if circulating_supply else None
    volume_to_mcap = None
    if circulating_market_cap and quote.volume_24h:
        volume_to_mcap = quote.volume_24h * quote.price / circulating_market_cap * 100.0
    return MarketCapMetrics(
        circulating_supply=circulating_supply,
        total_supply=total_supply,
        max_supply=max_supply,
        fdv=live_fdv,
        circulating_market_cap=circulating_market_cap,
        volume_to_market_cap=volume_to_mcap,
        coingecko_id=getattr(metadata, "coingecko_id", None),
    )


def microcap_score(market_cap: float | None, threshold: float = 15_000_000.0) -> float:
    if market_cap is None or market_cap <= 0:
        return 0.0
    if market_cap <= 5_000_000:
        return 100.0
    if market_cap <= 10_000_000:
        return 88.0
    if market_cap <= threshold:
        return 74.0
    if market_cap <= threshold * 2:
        return 35.0
    return 0.0


def dormancy_score(candles: list[CandlePoint], now: datetime) -> float:
    last_24h = within(candles, now, timedelta(hours=24))
    last_7d = within(candles, now, timedelta(days=7))
    r24 = range_pct(last_24h)
    r7 = range_pct(last_7d)
    if r24 is None or r7 is None:
        return 0.0
    quiet_range = clamp(100.0 - r24 * 10.0)
    historical_pop = clamp((r7 - r24) * 4.0)
    return clamp(quiet_range * 0.65 + historical_pop * 0.35)


def microcap_tags(metrics: MarketCapMetrics, threshold: float) -> list[str]:
    tags: list[str] = []
    cap = metrics.circulating_market_cap
    if cap is None:
        return tags
    if cap < threshold:
        tags.append("microcap")
    if cap < 10_000_000:
        tags.append("sub_10m")
    if cap < 5_000_000:
        tags.append("sub_5m")
    if metrics.volume_to_market_cap and metrics.volume_to_market_cap >= 5.0:
        tags.append("high_turnover")
    return tags
