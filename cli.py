from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta

from app.alerts.discord import DiscordWebhookClient
from app.analysis.liquidity import metrics_from_summary
from app.analysis.microcaps import market_cap_metrics_from_metadata
from app.analysis.structure import analyze_structure
from app.analysis.targets import generate_targets
from app.analysis.volume import calculate_volume_metrics
from app.coinbase.candles import bootstrap_product_history
from app.coinbase.products import canonicalize_spot_products
from app.coinbase.rest import CoinbaseRestClient
from app.coinbase.websocket import CoinbaseWebSocketClient
from app.config import Settings, get_settings
from app.database.connection import init_db, session_scope
from app.database.repositories import CatalystRepository, MarketRepository, MetadataRepository, ProductRepository, StatusRepository
from app.formatting import compact_money, money, pct, ratio
from app.logging import configure_logging
from app.metadata import CoinGeckoMetadataClient
from app.performance.statistics import performance_report
from app.scanner import ScannerService
from app.timeutils import utc_now


async def cmd_run(_: argparse.Namespace, settings: Settings) -> None:
    service = ScannerService(settings)
    await service.run_forever()


async def cmd_status(_: argparse.Namespace, settings: Settings) -> None:
    database_ok = False
    rest_ok = False
    ws_ok = False
    quote_age = None
    assets_monitored = 0
    ws_product = "BTC-USD"
    await init_db(settings)
    database_ok = True
    statuses = {}
    try:
        async with CoinbaseRestClient(settings) as rest:
            raw = await rest.list_products()
        products = canonicalize_spot_products(raw)
        assets_monitored = len(products)
        rest_ok = assets_monitored > 0
        if products:
            ids = {product.product_id for product in products}
            if ws_product not in ids:
                ws_product = products[0].product_id
        async with session_scope(settings) as session:
            await ProductRepository(session).upsert_products(products)
            statuses = await StatusRepository(session).all_status()
    except Exception as exc:  # noqa: BLE001
        print(f"coinbase REST error: {type(exc).__name__}")

    if rest_ok:
        quote, received_quote_age = await probe_websocket_quote(ws_product)
        if quote is not None:
            ws_ok = True
            quote_age = received_quote_age
            async with session_scope(settings) as session:
                await MarketRepository(session).insert_tick(quote)

    print("Coinbase Early Move Scanner status")
    print(f"database: {'PASS' if database_ok else 'FAIL'}")
    print(f"coinbase_rest: {'PASS' if rest_ok else 'FAIL'}")
    print(f"coinbase_websocket: {'PASS' if ws_ok else 'FAIL'}")
    print(f"live_quote_age_seconds: {quote_age:.3f}" if quote_age is not None else "live_quote_age_seconds: n/a")
    print(f"assets_monitored: {assets_monitored}")
    print(f"discord_alerts_configured: {'YES' if settings.alert_webhook else 'NO'}")
    for key, value in statuses.items():
        print(f"{key}: {value}")

async def cmd_scan(_: argparse.Namespace, settings: Settings) -> None:
    service = ScannerService(settings)
    await init_db(settings)
    await service.refresh_products()
    await hydrate_missing_quotes(service, settings)
    candidates = await service.scan_once(persist_and_alert=False)
    if not candidates:
        print("No qualifying candidates. Discord silence is correct.")
        return
    for candidate in candidates:
        print_candidate(candidate)


async def cmd_coin(args: argparse.Namespace, settings: Settings) -> None:
    symbol = args.symbol.upper()
    service = ScannerService(settings)
    await init_db(settings)
    await service.refresh_products()
    product_id = service.asset_to_product_id.get(symbol)
    if not product_id:
        print(f"{symbol} is not in the current qualifying Coinbase USD/USDC spot universe.")
        return
    await hydrate_product(product_id, settings)
    await hydrate_metadata([symbol], settings)
    candidates = await service.scan_once(persist_and_alert=False)
    for candidate in candidates:
        if candidate.symbol == symbol:
            print_candidate(candidate, verbose=True)
            return
    async with session_scope(settings) as session:
        market_repo = MarketRepository(session)
        quote = await market_repo.latest_quote(product_id)
        candles = await market_repo.recent_candles(product_id, utc_now() - timedelta(days=31))
        metadata = await MetadataRepository(session).get_metadata(symbol)
        book = await market_repo.latest_order_book_summary(product_id)
    if quote is None:
        print(f"No live Coinbase quote available for {symbol} yet.")
        return
    volume = calculate_volume_metrics(candles, utc_now())
    structure = analyze_structure(candles, quote.price, utc_now())
    cap = market_cap_metrics_from_metadata(metadata, quote)
    liquidity = metrics_from_summary(book.raw_summary if book else None)
    print(f"{symbol} / {product_id}")
    print(f"price: {money(quote.price)}")
    print(f"quote age: {quote.quote_age_seconds if quote.quote_age_seconds is not None else 'unknown'} sec")
    print(f"market cap: {compact_money(cap.circulating_market_cap)}")
    print(f"24h vol / market cap: {pct(cap.volume_to_market_cap, signed=False)}")
    print(f"5m volume: {ratio(volume.ratios.get('5m_vs_baseline'))}")
    print(f"1h volume vs 7d: {ratio(volume.ratios.get('1h_vs_7d'))}")
    print(f"base: {money(structure.base_low)}-{money(structure.base_high)}")
    print(f"breakout: {money(structure.breakout_trigger)}")
    print(f"invalidation: {money(structure.invalidation)}")
    print(f"spread: {pct(liquidity.spread_pct, signed=False)}")


async def cmd_microcaps(_: argparse.Namespace, settings: Settings) -> None:
    await init_db(settings)
    await hydrate_metadata_from_products(settings)
    async with session_scope(settings) as session:
        products = await ProductRepository(session).list_active_products()
        metadata = await MetadataRepository(session).all_metadata()
        market_repo = MarketRepository(session)
        rows = []
        for product in products:
            quote = await market_repo.latest_quote(product.product_id)
            if quote is None:
                continue
            cap = market_cap_metrics_from_metadata(metadata.get(product.base_currency), quote)
            if cap.circulating_market_cap and cap.circulating_market_cap <= settings.microcap_threshold_usd:
                rows.append((cap.circulating_market_cap, product.base_currency, product.product_id, cap.volume_to_market_cap))
    rows.sort()
    if not rows:
        print("No sub-threshold Coinbase microcaps with local quote + supply metadata yet.")
        return
    for cap, symbol, product_id, turnover in rows:
        print(f"{symbol:8} {product_id:14} cap={compact_money(cap):>10} vol/mcap={pct(turnover, signed=False)}")


async def cmd_catalysts(_: argparse.Namespace, settings: Settings) -> None:
    await init_db(settings)
    service = ScannerService(settings)
    await service.refresh_products()
    service.catalysts.set_known_assets(service.asset_to_product_id.keys())
    inserted = await service.catalysts.poll_once()
    since = utc_now() - timedelta(minutes=settings.catalyst_fresh_window_minutes)
    async with session_scope(settings) as session:
        rows = await CatalystRepository(session).fresh_catalysts(since)
    print(f"new catalysts inserted: {inserted}")
    for row in rows[:25]:
        assets = ",".join(row.affected_assets)
        print(f"{row.first_detected_time.isoformat()} [{row.source}] {assets} {row.catalyst_type} {row.strength:.0f} - {row.headline} {row.url}")


async def cmd_performance(_: argparse.Namespace, settings: Settings) -> None:
    await init_db(settings)
    print(await performance_report(days=7))
    print()
    print(await performance_report(days=30))


async def cmd_test_discord(_: argparse.Namespace, settings: Settings) -> None:
    async with DiscordWebhookClient(settings) as discord:
        message_id = await discord.send_test_message()
    print(f"test message sent: {message_id or 'no id returned'}")


async def hydrate_missing_quotes(service: ScannerService, settings: Settings) -> None:
    for product_id in list(service.products):
        await hydrate_product(product_id, settings, bootstrap=False)


async def hydrate_product(product_id: str, settings: Settings, bootstrap: bool = True) -> None:
    async with CoinbaseRestClient(settings) as rest:
        quote = await rest.get_ticker(product_id)
        async with session_scope(settings) as session:
            repo = MarketRepository(session)
            if quote:
                await repo.insert_tick(quote)
            if bootstrap:
                await bootstrap_product_history(rest, repo, product_id, settings=settings)


async def hydrate_metadata_from_products(settings: Settings) -> None:
    async with session_scope(settings) as session:
        products = await ProductRepository(session).list_active_products()
    await hydrate_metadata([product.base_currency for product in products], settings)


async def hydrate_metadata(symbols: list[str], settings: Settings) -> None:
    async with CoinGeckoMetadataClient(settings) as client:
        data = await client.fetch_assets(symbols)
    async with session_scope(settings) as session:
        repo = MetadataRepository(session)
        for asset, (metrics, raw) in data.items():
            await repo.upsert_metadata(asset, metrics, raw=raw)

async def probe_websocket_quote(product_id: str):
    seen = asyncio.Event()
    holder = {"quote": None, "quote_age": None}

    async def on_quote(quote):
        age = quote.quote_age_seconds
        holder["quote"] = quote
        holder["quote_age"] = age
        if age is not None and age <= 2.0:
            seen.set()

    client = CoinbaseWebSocketClient([product_id], on_quote=on_quote)
    await client.start()
    try:
        await asyncio.wait_for(seen.wait(), timeout=20)
    except asyncio.TimeoutError:
        return holder["quote"], holder["quote_age"]
    finally:
        await client.stop()
    return holder["quote"], holder["quote_age"]

def print_candidate(candidate, verbose: bool = False) -> None:
    print(f"{candidate.symbol} - {candidate.status.value} score={candidate.score.early_move_score:.0f}")
    print(f"price={money(candidate.quote.price)}  24h={pct(candidate.quote.price_change_24h_pct)}  quote_age={candidate.quote.quote_age_seconds:.1f}s")
    print(f"market_cap={compact_money(candidate.market_cap.circulating_market_cap)} vol/mcap={pct(candidate.market_cap.volume_to_market_cap, signed=False)}")
    print(f"5m={ratio(candidate.volume.ratios.get('5m_vs_baseline'))} 15m={ratio(candidate.volume.ratios.get('15m_vs_baseline'))} 1h={ratio(candidate.volume.ratios.get('1h_vs_7d'))} 4h={ratio(candidate.volume.ratios.get('4h_vs_7d'))}")
    print(f"base={money(candidate.structure.base_low)}-{money(candidate.structure.base_high)} breakout={money(candidate.structure.breakout_trigger)} invalidation={money(candidate.structure.invalidation)}")
    print(f"TP1={money(candidate.targets.tp1)} TP2={money(candidate.targets.tp2)} Stretch={money(candidate.targets.stretch)}")
    print("why now: " + " ".join(candidate.score.reasons))
    if verbose:
        print(f"components={candidate.score.components}")
        print(f"penalties={candidate.score.penalties}")
        print(f"risks={candidate.score.risks}")
    print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coinbase Early Move Scanner")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    sub.add_parser("scan")
    coin = sub.add_parser("coin")
    coin.add_argument("symbol")
    sub.add_parser("microcaps")
    sub.add_parser("catalysts")
    sub.add_parser("performance")
    sub.add_parser("test-discord")
    return parser


async def dispatch(args: argparse.Namespace, settings: Settings) -> None:
    commands = {
        "run": cmd_run,
        "status": cmd_status,
        "scan": cmd_scan,
        "coin": cmd_coin,
        "microcaps": cmd_microcaps,
        "catalysts": cmd_catalysts,
        "performance": cmd_performance,
        "test-discord": cmd_test_discord,
    }
    await commands[args.command](args, settings)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(dispatch(args, settings))


if __name__ == "__main__":
    main()
