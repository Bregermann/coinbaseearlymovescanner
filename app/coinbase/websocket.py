from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

import websockets
from websockets.client import WebSocketClientProtocol

from app.coinbase.orderbook import OrderBookManager
from app.coinbase.rest import candle_from_coinbase
from app.coinbase.trades import TradeCandleBuilder, parse_trade
from app.config import Settings, get_settings
from app.timeutils import parse_coinbase_time, utc_now
from app.types import CandlePoint, LiveQuote

logger = logging.getLogger(__name__)

QuoteCallback = Callable[[LiveQuote], Awaitable[None]]
CandleCallback = Callable[[CandlePoint], Awaitable[None]]
BookCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
StatusCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


class CoinbaseWebSocketClient:
    def __init__(
        self,
        product_ids: Sequence[str],
        settings: Settings | None = None,
        on_quote: QuoteCallback | None = None,
        on_candle: CandleCallback | None = None,
        on_book_summary: BookCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.product_ids = list(dict.fromkeys(product_ids))
        self.on_quote = on_quote
        self.on_candle = on_candle
        self.on_book_summary = on_book_summary
        self.on_status = on_status
        self.order_books = OrderBookManager()
        self.trade_candles = TradeCandleBuilder()
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._last_book_emit: dict[str, datetime] = {}
        self.connected_chunks = 0
        self.last_message_at: datetime | None = None
        self.last_trade_at: datetime | None = None
        self.last_trade_product_id: str | None = None

    async def start(self) -> None:
        self._stop.clear()
        for chunk in _chunks(self.product_ids, self.settings.coinbase_ws_product_chunk_size):
            task = asyncio.create_task(self._run_chunk(chunk), name=f"coinbase-ws-{chunk[0]}")
            self._tasks.append(task)

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run_chunk(self, product_ids: list[str]) -> None:
        delay = self.settings.coinbase_ws_reconnect_min_seconds
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.settings.coinbase_ws_url, ping_interval=20, ping_timeout=20, max_size=self.settings.coinbase_ws_max_message_size_bytes) as ws:
                    self.connected_chunks += 1
                    delay = self.settings.coinbase_ws_reconnect_min_seconds
                    await self._subscribe(ws, product_ids)
                    logger.info("coinbase_ws_connected", extra={"_products": len(product_ids)})
                    async for raw in ws:
                        self.last_message_at = utc_now()
                        try:
                            await self._handle_message(raw)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("coinbase_ws_message_handler_failed", extra={"_error_type": type(exc).__name__})
                        if self._stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("coinbase_ws_disconnected", extra={"_error": str(exc), "_products": len(product_ids)})
            finally:
                self.connected_chunks = max(0, self.connected_chunks - 1)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.settings.coinbase_ws_reconnect_max_seconds)

    async def _subscribe(self, ws: WebSocketClientProtocol, product_ids: list[str]) -> None:
        channels = ["heartbeats", "ticker", "market_trades", "status"]
        if self.settings.enable_candles_channel:
            channels.append("candles")
        if self.settings.enable_level2:
            channels.append("level2")
        for channel in channels:
            message: dict[str, Any] = {"type": "subscribe", "channel": channel}
            if channel != "heartbeats":
                message["product_ids"] = product_ids
            await ws.send(json.dumps(message))

    async def _handle_message(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("coinbase_ws_bad_json")
            return
        channel = message.get("channel") or message.get("type")
        timestamp = message.get("timestamp")
        if channel == "ticker":
            await self._handle_ticker(message, timestamp)
        elif channel == "market_trades":
            await self._handle_trades(message)
        elif channel == "candles":
            await self._handle_candles(message)
        elif channel in {"level2", "l2_data"}:
            await self._handle_level2(message, timestamp)
        elif channel == "status" and self.on_status:
            for event in message.get("events", []) or []:
                await self.on_status("coinbase_status", event)
        elif channel == "heartbeats" and self.on_status:
            await self.on_status("coinbase_heartbeat", {"timestamp": timestamp, "events": message.get("events", [])})

    async def _handle_ticker(self, message: dict[str, Any], fallback_time: str | None) -> None:
        if not self.on_quote:
            return
        exchange_time = parse_coinbase_time(fallback_time)
        now = utc_now()
        for event in message.get("events", []) or []:
            for ticker in event.get("tickers", []) or []:
                product_id = str(ticker.get("product_id") or "").upper()
                price = _to_float(ticker.get("price"))
                if not product_id or price is None:
                    continue
                best_bid = _to_float(ticker.get("best_bid"))
                best_ask = _to_float(ticker.get("best_ask"))
                if best_bid is None or best_ask is None:
                    book_bid, book_ask = self.order_books.get_best(product_id)
                    best_bid = best_bid or book_bid
                    best_ask = best_ask or book_ask
                quote = LiveQuote(
                    product_id=product_id,
                    price=price,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    volume_24h=_to_float(ticker.get("volume_24_h") or ticker.get("volume_24h")),
                    price_change_24h_pct=_to_float(ticker.get("price_percent_chg_24_h") or ticker.get("price_change_24_h")),
                    exchange_time=parse_coinbase_time(ticker.get("time")) or exchange_time,
                    ingestion_time=now,
                    last_trade_time=parse_coinbase_time(ticker.get("time")) or exchange_time,
                )
                await self.on_quote(quote)

    async def _handle_trades(self, message: dict[str, Any]) -> None:
        if not self.on_candle:
            return
        for event in message.get("events", []) or []:
            for row in event.get("trades", []) or []:
                trade = parse_trade(row)
                if trade is None:
                    continue
                self.last_trade_at = trade.trade_time
                self.last_trade_product_id = trade.product_id
                closed = self.trade_candles.ingest(trade)
                if closed is not None:
                    await self.on_candle(closed)

    async def _handle_candles(self, message: dict[str, Any]) -> None:
        if not self.on_candle:
            return
        for event in message.get("events", []) or []:
            for row in event.get("candles", []) or []:
                product_id = str(row.get("product_id") or "").upper()
                if not product_id:
                    continue
                candle = candle_from_coinbase(product_id, row, granularity_seconds=300)
                if candle:
                    await self.on_candle(candle)

    async def _handle_level2(self, message: dict[str, Any], fallback_time: str | None) -> None:
        if not self.on_book_summary:
            return
        for event in message.get("events", []) or []:
            summary = self.order_books.apply_event(event, fallback_time=fallback_time)
            if summary is None:
                continue
            product_id = summary["product_id"]
            now = utc_now()
            last = self._last_book_emit.get(product_id)
            if last and (now - last).total_seconds() < 5:
                continue
            self._last_book_emit[product_id] = now
            await self.on_book_summary(product_id, summary)


def _chunks(items: Sequence[str], size: int) -> list[list[str]]:
    size = max(1, size)
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
