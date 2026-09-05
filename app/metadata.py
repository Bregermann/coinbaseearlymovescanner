from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable

import httpx

from app.config import Settings, get_settings
from app.types import MarketCapMetrics

logger = logging.getLogger(__name__)


class CoinGeckoMetadataClient:
    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "CoinGeckoMetadataClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.coingecko_timeout_seconds)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.coingecko_timeout_seconds)
        return self._client

    async def fetch_assets(self, assets: Iterable[str]) -> dict[str, tuple[MarketCapMetrics, dict[str, Any]]]:
        assets_upper = sorted({asset.upper() for asset in assets if asset and asset.isascii()})
        results: dict[str, tuple[MarketCapMetrics, dict[str, Any]]] = {}
        for batch in _chunks(assets_upper, 50):
            rows = await self._fetch_market_rows(batch)
            by_symbol: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                symbol = str(row.get("symbol") or "").upper()
                by_symbol.setdefault(symbol, []).append(row)
            for symbol in batch:
                matches = by_symbol.get(symbol, [])
                if not matches:
                    continue
                row = matches[0]
                metrics = MarketCapMetrics(
                    circulating_supply=_to_float(row.get("circulating_supply")),
                    total_supply=_to_float(row.get("total_supply")),
                    max_supply=_to_float(row.get("max_supply")),
                    fdv=_to_float(row.get("fully_diluted_valuation")),
                    circulating_market_cap=None,
                    volume_to_market_cap=None,
                    coingecko_id=str(row.get("id")) if row.get("id") else None,
                )
                results[symbol] = (metrics, row)
        return results

    async def _fetch_market_rows(self, symbols: list[str]) -> list[dict[str, Any]]:
        if not symbols:
            return []
        try:
            response = await self.client.get(
                f"{self.settings.coingecko_base_url.rstrip('/')}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "symbols": ",".join(symbols).lower(),
                    "include_tokens": "all",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": 1,
                    "sparkline": "false",
                },
                headers={"accept": "application/json"},
            )
        except httpx.RequestError as exc:
            logger.warning("coingecko_metadata_request_error", extra={"_error_type": type(exc).__name__, "_batch_size": len(symbols)})
            await asyncio.sleep(1)
            return []
        if response.status_code == 400 and len(symbols) > 1:
            midpoint = len(symbols) // 2
            left = await self._fetch_market_rows(symbols[:midpoint])
            right = await self._fetch_market_rows(symbols[midpoint:])
            return left + right
        if response.status_code >= 400:
            logger.warning("coingecko_metadata_error", extra={"_status_code": response.status_code, "_batch_size": len(symbols)})
            await asyncio.sleep(1)
            return []
        try:
            rows = response.json()
        except ValueError:
            logger.warning("coingecko_metadata_bad_json", extra={"_batch_size": len(symbols)})
            return []
        return rows if isinstance(rows, list) else []


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
