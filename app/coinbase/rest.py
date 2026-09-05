from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import httpx

from app.config import Settings, get_settings
from app.timeutils import parse_coinbase_time, utc_now
from app.types import CandlePoint, LiveQuote

logger = logging.getLogger(__name__)

GRANULARITY_SECONDS = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 300,
    "FIFTEEN_MINUTE": 900,
    "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600,
    "TWO_HOUR": 7200,
    "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
}


class CoinbaseRestClient:
    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "CoinbaseRestClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.coinbase_rest_timeout_seconds)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.coinbase_rest_timeout_seconds)
        return self._client

    async def request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.settings.coinbase_rest_base_url.rstrip('/')}/{path.lstrip('/')}"
        last_error: Exception | None = None
        for attempt in range(self.settings.coinbase_rest_max_retries):
            try:
                response = await self.client.get(url, params=params, headers={"cache-control": "no-cache"})
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                delay = min(2**attempt, 16) + attempt * 0.1
                logger.warning("coinbase_rest_error", extra={"_path": path, "_attempt": attempt + 1, "_error": str(exc)})
                await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error

    async def list_products(self) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"product_type": "SPOT", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            payload = await self.request("/market/products", params=params)
            products.extend(payload.get("products") or payload.get("data") or [])
            cursor = payload.get("cursor") or payload.get("next_cursor") or payload.get("next")
            if not cursor:
                break
        return products

    async def get_product(self, product_id: str) -> dict[str, Any]:
        return await self.request(f"/market/products/{product_id}")

    async def get_ticker(self, product_id: str) -> LiveQuote | None:
        payload = await self.request(f"/market/products/{product_id}/ticker", params={"limit": 1})
        trades = payload.get("trades") or []
        trade = trades[0] if trades else {}
        price = _to_float(trade.get("price") or payload.get("price"))
        if price is None:
            return None
        exchange_time = parse_coinbase_time(trade.get("time") or payload.get("time"))
        now = utc_now()
        return LiveQuote(
            product_id=product_id,
            price=price,
            best_bid=None,
            best_ask=None,
            volume_24h=None,
            price_change_24h_pct=None,
            exchange_time=exchange_time,
            ingestion_time=now,
            last_trade_time=exchange_time,
        )

    async def get_public_book(self, product_id: str, limit: int = 100) -> dict[str, Any]:
        return await self.request("/market/product_book", params={"product_id": product_id, "limit": limit})

    async def get_candles(
        self,
        product_id: str,
        start: datetime,
        end: datetime,
        granularity: str = "ONE_MINUTE",
    ) -> list[CandlePoint]:
        params = {
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
            "granularity": granularity,
        }
        payload = await self.request(f"/market/products/{product_id}/candles", params=params)
        seconds = GRANULARITY_SECONDS[granularity]
        candles = payload.get("candles") or []
        parsed: list[CandlePoint] = []
        for row in candles:
            candle = candle_from_coinbase(product_id, row, seconds)
            if candle:
                parsed.append(candle)
        parsed.sort(key=lambda item: item.start)
        return parsed

    async def iter_candle_ranges(
        self,
        product_id: str,
        start: datetime,
        end: datetime,
        granularity: str,
        max_candles: int = 300,
    ) -> AsyncIterator[list[CandlePoint]]:
        seconds = GRANULARITY_SECONDS[granularity]
        cursor = start
        step = timedelta(seconds=seconds * max_candles)
        while cursor < end:
            chunk_end = min(end, cursor + step)
            yield await self.get_candles(product_id, cursor, chunk_end, granularity=granularity)
            cursor = chunk_end


def candle_from_coinbase(product_id: str, row: dict[str, Any], granularity_seconds: int) -> CandlePoint | None:
    start_raw = row.get("start")
    try:
        start = datetime.fromtimestamp(int(start_raw), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    open_price = _to_float(row.get("open"))
    high = _to_float(row.get("high"))
    low = _to_float(row.get("low"))
    close = _to_float(row.get("close"))
    volume = _to_float(row.get("volume"))
    if None in {open_price, high, low, close, volume}:
        return None
    assert open_price is not None and high is not None and low is not None and close is not None and volume is not None
    return CandlePoint(
        product_id=product_id,
        start=start,
        granularity_seconds=granularity_seconds,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        quote_volume=volume * close,
    )


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
