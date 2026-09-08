from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.alerts.discord import DiscordWebhookClient
from app.analysis.bottoms import analyze_bottom_structure, candidate_alert_stage
from app.analysis.fibonacci import analyze_fibonacci
from app.analysis.liquidity import metrics_from_summary
from app.analysis.microcaps import market_cap_metrics_from_metadata
from app.analysis.portfolio_rotation import assess_holding
from app.analysis.scoring import book_depth_usd, dollar_volume_24h, market_cap_class, primary_score
from app.analysis.structure import analyze_structure
from app.analysis.targets import generate_targets
from app.analysis.validation import validate_bottom_history
from app.analysis.volume import calculate_volume_metrics
from app.coinbase.candles import bootstrap_product_history
from app.coinbase.products import canonicalize_spot_products
from app.coinbase.rest import CoinbaseRestClient
from app.coinbase.websocket import CoinbaseWebSocketClient
from app.config import Settings, get_settings
from sqlalchemy import func, select

from app.database.connection import close_db, init_db, session_scope
from app.database.models import AlertModel, CandleModel, CandidateSnapshotModel, MarketTickModel, OrderBookSnapshotModel, ProductModel
from app.database.repositories import CatalystRepository, MarketRepository, MetadataRepository, ProductRepository, StatusRepository
from app.diagnostics import candidate_diagnostic_dict, diagnostics_status
from app.formatting import compact_money, money, pct, ratio
from app.logging import configure_logging
from app.metadata import CoinGeckoMetadataClient
from app.performance.statistics import performance_report
from app.performance.rotation import rotation_backtest_report
from app.portfolio import load_portfolio
from app.scanner import ScannerService
from app.timeutils import human_duration, utc_now


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
    bottom = analyze_bottom_structure(candles, quote.price, utc_now(), liquidity=liquidity)
    fib = analyze_fibonacci(
        candles,
        quote.price,
        utc_now(),
        structure=structure,
        bottom=bottom,
        minimum_confidence=settings.fib_min_swing_confidence,
        probability_min_samples=settings.fib_probability_min_samples,
    )
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
    print(f"bottom stage: {bottom.stage.value}")
    print(f"pattern: {bottom.pattern}")
    print(f"bottom_score: {bottom.bottom_score:.2f}")
    print(f"breakout_score: {bottom.breakout_score:.2f}")
    print(f"distance_to_breakout: {format_pct_value(bottom.distance_to_breakout_pct)}")
    print(
        f"fib: {fib.signal} ({fib.timeframe}, confidence {fib.swing_confidence:.2f}, "
        f"confluence {fib.confluence_score:.2f})"
        if fib.reliable else "fib: N/A"
    )


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


async def cmd_rotation_backtest(args: argparse.Namespace, settings: Settings) -> None:
    await init_db(settings)
    print(await rotation_backtest_report(settings, days=args.days))


async def cmd_portfolio(_: argparse.Namespace, settings: Settings) -> None:
    holdings = load_portfolio(settings)
    if not holdings:
        print("portfolio: not configured (no holdings were inferred)")
        return
    service = ScannerService(settings)
    await init_db(settings)
    await service.refresh_products()
    async with session_scope(settings) as session:
        diagnostics, _skipped = await service.evaluate_candidates(session, utc_now(), include_stale=True)
    candidates = {item.candidate.symbol: item.candidate for item in diagnostics}
    print(f"portfolio_holdings: {len(holdings)}")
    for holding in holdings:
        assessment = assess_holding(holding, candidates.get(holding.ticker))
        state = "ROTATION_PROTECTED" if assessment.protected else "eligible for comparison"
        print(
            f"{assessment.ticker}: hold_score={assessment.hold_score:.2f} "
            f"deterioration={assessment.deterioration_score:.2f} "
            f"risk_reward={format_optional(assessment.risk_reward)} state={state}"
        )


async def cmd_test_discord(_: argparse.Namespace, settings: Settings) -> None:
    async with DiscordWebhookClient(settings) as discord:
        message_id = await discord.send_test_message()
    print(f"test message sent: {message_id or 'no id returned'}")


async def cmd_diagnostics(_: argparse.Namespace, settings: Settings) -> None:
    await init_db(settings)
    service = ScannerService(settings)
    await service.refresh_products()
    now = utc_now()
    async with session_scope(settings) as session:
        diagnostics, skipped = await service.evaluate_candidates(session, now, include_stale=True)
        summary = diagnostics_status(diagnostics, skipped, products_checked=len(service.products), settings=settings)
        statuses = await StatusRepository(session).all_status()
        baseline = baseline_summary_from_cache(
            service._analysis_candle_cache,
            list(service.products),
            now,
        )
        alert_counts = await alert_count_summary(session, now)
        market_state = await market_state_summary(session, now, settings, statuses)
        active_alerts = (await session.execute(select(func.count()).select_from(AlertModel).where(AlertModel.is_active.is_(True)))).scalar_one()
        missing_discord_ids = (
            await session.execute(select(func.count()).select_from(AlertModel).where(AlertModel.discord_message_id.is_(None)))
        ).scalar_one()

    runtime = statuses.get("runtime", {}).get("value", {})
    websocket = statuses.get("websocket", {}).get("value", {})
    scan = statuses.get("scan", {}).get("value", {})
    started_at = parse_datetime(runtime.get("started_at")) if isinstance(runtime, dict) else None
    uptime_seconds = (now - started_at).total_seconds() if started_at else None
    ws_connected = bool(websocket.get("connected_chunks", 0)) if isinstance(websocket, dict) else False
    heartbeat_at = None
    if isinstance(websocket, dict):
        heartbeat = (websocket.get("ws_status", {}) or {}).get("coinbase_heartbeat", {})
        heartbeat_at = parse_datetime(heartbeat.get("updated_at")) if isinstance(heartbeat, dict) else None
    if heartbeat_at and (now - heartbeat_at).total_seconds() <= 90:
        ws_connected = True

    print("Coinbase Early Move Scanner diagnostics")
    print(f"scanner_uptime: {human_duration(uptime_seconds)}")
    print(f"coinbase_connection: {'CONNECTED' if ws_connected else 'DISCONNECTED'}")
    print(f"quote_age_seconds: {market_state['quote_age_seconds']}")
    print(f"assets_monitored: {len(service.products)}")
    print(f"assets_subscribed: {websocket.get('assets_subscribed', len(service.products)) if isinstance(websocket, dict) else len(service.products)}")
    print(f"assets_receiving_live_data: {market_state['assets_receiving_live_data']}")
    print(f"assets_with_complete_baselines: {baseline['complete_30d']}")
    print(f"assets_with_incomplete_baselines: {baseline['incomplete_30d']}")
    print(f"assets_failing_scoring_missing_history: {baseline['no_history'] + summary['suppressed_by'].get('insufficient history', 0)}")
    print(f"last_coinbase_message: {websocket.get('last_coinbase_message_at', 'n/a') if isinstance(websocket, dict) else 'n/a'}")
    print(f"last_ticker: {format_event_marker(websocket.get('last_ticker')) if isinstance(websocket, dict) else 'n/a'}")
    print(f"last_trade: {format_event_marker(websocket.get('last_trade')) if isinstance(websocket, dict) else 'n/a'}")
    print(f"last_candle: {format_event_marker(websocket.get('last_candle')) if isinstance(websocket, dict) else 'n/a'}")
    print(f"database_last_write: {market_state['database_last_write']}")
    print(f"discord_alert_webhook_url: {'configured' if settings.alert_webhook else 'missing'}")
    print(f"debug_heartbeat_webhook_url: {'configured' if settings.debug_webhook else 'missing'}")
    portfolio = statuses.get("portfolio_rotation", {}).get("value", {})
    print(f"portfolio_configured: {'YES' if portfolio.get('configured') else 'NO'}")
    print(f"portfolio_holdings: {portfolio.get('holdings', 0)}")
    print(f"portfolio_holdings_evaluated: {portfolio.get('evaluated_holdings', 0)}")
    print(f"rotation_actionable_candidates: {portfolio.get('actionable_candidates', 0)}")
    print(f"candidates_evaluated_last_minute: {summary['candidates_evaluated']}")
    print(f"candidates_score_gte_50: {summary['score_counts']['gte_50']}")
    print(f"candidates_score_gte_60: {summary['score_counts']['gte_60']}")
    print(f"candidates_score_gte_70: {summary['score_counts']['gte_70']}")
    print(f"candidates_score_gte_80: {summary['score_counts']['gte_80']}")
    print(f"bottom_forming_now: {summary['stage_counts'].get('BOTTOM FORMING', 0)}")
    print(f"pre_breakout_now: {summary['stage_counts'].get('PRE-BREAKOUT', 0)}")
    print(f"breakout_firing_now: {summary['stage_counts'].get('BREAKOUT FIRING', 0)}")
    print(f"retest_hold_now: {summary['stage_counts'].get('RETEST HOLD', 0)}")
    print(f"qualifying_alerts_now: {summary['would_alert_now']}")
    print(f"alerts_last_1h: {alert_counts['1h']}")
    print(f"alerts_last_4h: {alert_counts['4h']}")
    print(f"alerts_last_24h: {alert_counts['24h']}")
    print(f"active_alerts: {active_alerts}")
    print(f"alerts_missing_discord_message_id: {missing_discord_ids}")
    print(f"last_scan_at: {scan.get('last_scan_at', 'n/a') if isinstance(scan, dict) else 'n/a'}")
    print("alerts_suppressed_by:")
    for reason, count in sorted(summary["suppressed_by"].items(), key=lambda item: (-item[1], item[0])):
        print(f"  {reason}: {count}")
    print("top_10_current_candidates:")
    for item in diagnostics[:10]:
        print_diagnostic_candidate(item, settings)


async def cmd_validate_bottoms(args: argparse.Namespace, settings: Settings) -> None:
    await init_db(settings)
    now = utc_now()
    symbols = [value.upper() for value in args.symbols]
    requested = set(symbols) | {"BTC", "ETH"}
    async with session_scope(settings) as session:
        rows = list(
            (
                await session.execute(
                    select(ProductModel).where(
                        ProductModel.base_currency.in_(requested),
                        ProductModel.quote_currency.in_({"USD", "USDC"}),
                    )
                )
            ).scalars()
        )
        products = {}
        for row in sorted(rows, key=lambda item: (item.base_currency, item.quote_currency != "USD")):
            products.setdefault(row.base_currency, row.product_id)
        candle_map = {
            asset: await MarketRepository(session).recent_candles(
                product_id,
                now - timedelta(days=max(45, args.days + 10)),
            )
            for asset, product_id in products.items()
        }

    benchmarks = {
        asset: candle_map[asset]
        for asset in ("BTC", "ETH")
        if asset in candle_map
    }
    print("Coinbase bottom / pre-breakout validation")
    print("Replay rule: closed candles only; no future candle data is visible to the detector.")
    for symbol in symbols:
        product_id = products.get(symbol)
        if not product_id:
            print(f"\n{symbol}: FAIL - no Coinbase USD/USDC spot product")
            continue
        result = validate_bottom_history(
            symbol,
            product_id,
            candle_map.get(symbol, []),
            benchmark_candles=benchmarks,
            now=now,
            settings=settings,
            breakout_lookback_days=args.days,
        )
        print(f"\n{symbol} ({product_id})")
        print(f"first alert timestamp: {format_datetime(result.first_alert_timestamp)}")
        print(f"price at first alert: {money(result.first_alert_price)}")
        print(f"breakout timestamp: {format_datetime(result.breakout_timestamp)}")
        print(f"breakout price: {money(result.breakout_price)}")
        print(f"maximum price after alert: {money(result.maximum_price_after_alert)}")
        print(f"maximum upside after alert: {format_pct_value(result.maximum_upside_after_alert_pct)}")
        print(f"alert stage: {result.alert_stage or 'NONE'}")
        print(f"bottom_score: {format_optional(result.bottom_score)}")
        print(f"breakout_score: {format_optional(result.breakout_score)}")
        print(f"alerted before breakout: {'YES' if result.alerted_before_breakout else 'NO'}")
        if result.failure_reason:
            print(f"failure reason: {result.failure_reason}")


async def baseline_summary(session, product_ids: list[str]) -> dict[str, int]:
    now = utc_now()
    since30 = now - timedelta(days=30)
    complete = 0
    incomplete = 0
    rows = await session.execute(
        select(
            CandleModel.product_id,
            func.min(CandleModel.start),
            func.count(),
        )
        .where(
            CandleModel.product_id.in_(product_ids),
            CandleModel.start >= since30,
            CandleModel.granularity_seconds == 3600,
        )
        .group_by(CandleModel.product_id)
    )
    history = {
        product_id: (earliest, count)
        for product_id, earliest, count in rows
    }
    for product_id, (earliest, _count) in history.items():
        earliest = ensure_utc(earliest)
        if earliest and earliest <= since30 + timedelta(hours=2):
            complete += 1
        else:
            incomplete += 1
    no_history = len(set(product_ids) - set(history))
    return {"complete_30d": complete, "incomplete_30d": incomplete, "no_history": no_history}


def baseline_summary_from_cache(
    candle_map: dict[str, list],
    product_ids: list[str],
    now: datetime,
) -> dict[str, int]:
    cutoff = now - timedelta(days=30)
    complete = incomplete = no_history = 0
    for product_id in product_ids:
        hourly = [
            candle for candle in candle_map.get(product_id, [])
            if candle.granularity_seconds == 3600
        ]
        if not hourly:
            no_history += 1
        elif min(candle.start for candle in hourly) <= cutoff + timedelta(hours=2):
            complete += 1
        else:
            incomplete += 1
    return {"complete_30d": complete, "incomplete_30d": incomplete, "no_history": no_history}


async def alert_count_summary(session, now: datetime) -> dict[str, int]:
    counts = {}
    for label, delta in {"1h": timedelta(hours=1), "4h": timedelta(hours=4), "24h": timedelta(hours=24)}.items():
        counts[label] = (
            await session.execute(select(func.count()).select_from(AlertModel).where(AlertModel.detection_time >= now - delta))
        ).scalar_one()
    return counts


async def market_state_summary(
    session,
    now: datetime,
    settings: Settings,
    statuses: dict | None = None,
) -> dict[str, str | int]:
    websocket = (statuses or {}).get("websocket", {}).get("value", {})
    if isinstance(websocket, dict):
        ticker = websocket.get("last_ticker") or {}
        received_at = parse_datetime(ticker.get("received_at")) if isinstance(ticker, dict) else None
        quote_age = (
            f"{max(0.0, (now - received_at).total_seconds()):.3f}"
            if received_at is not None else "n/a"
        )
        return {
            "quote_age_seconds": quote_age,
            "assets_receiving_live_data": int(websocket.get("assets_receiving_live_data", 0) or 0),
            "database_last_write": str(websocket.get("last_persist_at") or "n/a"),
        }

    latest_tick = (
        await session.execute(
            select(MarketTickModel)
            .order_by(MarketTickModel.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    quote_age = "n/a"
    latest_exchange = ensure_utc(latest_tick.exchange_time) if latest_tick else None
    if latest_exchange is not None:
        quote_age = f"{max(0.0, (now - latest_exchange).total_seconds()):.3f}"
    return {
        "quote_age_seconds": quote_age,
        "assets_receiving_live_data": 1 if latest_tick else 0,
        "database_last_write": (
            ensure_utc(latest_tick.ingestion_time).isoformat()
            if latest_tick else "n/a"
        ),
    }


def print_diagnostic_candidate(item, settings: Settings) -> None:
    row = candidate_diagnostic_dict(item, settings)
    c = item.candidate
    result = "WOULD ALERT" if item.should_alert else "NO ALERT"
    if item.qualifies and not item.should_alert:
        result = "QUALIFIES BUT SUPPRESSED"
    reasons = "; ".join(item.suppression_reasons or ["would alert"])
    warnings = "; ".join(item.warnings) if item.warnings else "none"
    print(
        f"  {c.symbol:8} price={money(c.quote.price)} 24h={pct(c.quote.price_change_24h_pct)} "
        f"class={row['market_cap_class']} mcap={compact_money(c.market_cap.circulating_market_cap)} "
        f"riskAdj={row['risk_adjusted_opportunity_score']:.2f} early={c.score.early_move_score:.2f} "
        f"stage={row['alert_stage']} pattern={row['pattern'] or 'N/A'} "
        f"bottom={format_optional(row['bottom_score'])} breakout={format_optional(row['breakout_score'])} "
        f"breakoutDistance={format_pct_value(row['distance_to_breakout_pct'])} "
        f"fib={row['fib_signal']} fibTF={row['fib_timeframe'] or 'N/A'} "
        f"fibConfidence={format_optional(row['fib_swing_confidence'])} fibConfluence={format_optional(row['fib_confluence_score'])} "
        f"rotation={row['rotation_classification'] or 'N/A'} source={row['best_rotation_source'] or 'N/A'} "
        f"advantage={format_optional(row['rotation_advantage'])} "
        f"tech={c.score.technical_score:.2f} cat={c.score.catalyst_score:.2f} "
        f"liqSafety={format_optional(row['liquidity_safety_score'])} 24hDollarVol={compact_money(row['dollar_volume_24h'])} "
        f"spread={pct(row['spread_pct'], signed=False)} depth1={compact_money(row['book_depth_1_pct'])} "
        f"CoinbaseTraders={format_traders(row['coinbase_traders'])} "
        f"vol={format_optional(row['volume_acceleration_score'])} GoonerEMA={format_optional(row['gooner_ema_score'])} "
        f"TraderAcceleration={format_optional(row['trader_acceleration_score'])} "
        f"UpsideRunwayScore={format_optional(row['upside_runway_score'])} "
        f"upside={format_pct_value(item.upside_runway_pct)} extPenalty={format_optional(row['extension_penalty'])} "
        f"badLiqPenalty={format_optional(row['bad_liquidity_penalty'])} thinPenalty={format_optional(row['thin_participation_penalty'])} "
        f"dilutionPenalty={format_optional(row['dilution_penalty'])} result={result} why={reasons} warnings={warnings}"
    )


def parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return ensure_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_event_marker(value: object) -> str:
    if not isinstance(value, dict):
        return "n/a"
    product_id = value.get("product_id") or "n/a"
    received = value.get("received_at") or "n/a"
    return f"{product_id} at {received}"


def format_optional(value: object) -> str:
    return "N/A" if value is None else f"{float(value):.2f}"


def format_traders(value: object) -> str:
    return "N/A" if value is None else f"{float(value):,.0f}"


def format_pct_value(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}%"


def format_datetime(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "n/a"


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
    print(f"{candidate.symbol} - {candidate_alert_stage(candidate)} risk_adjusted={primary_score(candidate):.0f} early={candidate.score.early_move_score:.0f}")
    print(f"price={money(candidate.quote.price)}  24h={pct(candidate.quote.price_change_24h_pct)}  quote_age={candidate.quote.quote_age_seconds:.1f}s")
    print(
        f"market_cap_class={market_cap_class(candidate.market_cap)} market_cap={compact_money(candidate.market_cap.circulating_market_cap)} "
        f"24h_dollar_volume={compact_money(dollar_volume_24h(candidate.volume))} vol/mcap={pct(candidate.market_cap.volume_to_market_cap, signed=False)}"
    )
    print(
        f"liquidity_safety={format_optional(candidate.score.components.get('LiquiditySafetyScore'))} "
        f"spread={pct(candidate.liquidity.spread_pct, signed=False)} depth1={compact_money(book_depth_usd(candidate.liquidity, '1'))} CoinbaseTraders=N/A"
    )
    print(f"5m={ratio(candidate.volume.ratios.get('5m_vs_baseline'))} 15m={ratio(candidate.volume.ratios.get('15m_vs_baseline'))} 1h={ratio(candidate.volume.ratios.get('1h_vs_7d'))} 4h={ratio(candidate.volume.ratios.get('4h_vs_7d'))}")
    print(f"base={money(candidate.structure.base_low)}-{money(candidate.structure.base_high)} breakout={money(candidate.structure.breakout_trigger)} invalidation={money(candidate.structure.invalidation)}")
    if candidate.bottom:
        print(
            f"pattern={candidate.bottom.pattern} bottom={candidate.bottom.bottom_score:.0f} "
            f"breakoutScore={candidate.bottom.breakout_score:.0f} "
            f"distance={format_pct_value(candidate.bottom.distance_to_breakout_pct)}"
        )
    if candidate.fib and candidate.fib.reliable:
        print(
            f"fib={candidate.fib.signal} timeframe={candidate.fib.timeframe} "
            f"confidence={candidate.fib.swing_confidence:.0f} confluence={candidate.fib.confluence_score:.0f} "
            f"support={money(candidate.fib.nearest_support)} resistance={money(candidate.fib.nearest_resistance)}"
        )
    else:
        print("fib=N/A (no reliable active impulse)")
    if candidate.rotation and candidate.rotation.best_source:
        source = candidate.rotation.best_source
        print(
            f"rotation={candidate.rotation.classification.value} source={source.ticker} "
            f"advantage={source.rotation_advantage:+.1f} suggested={source.suggested_percentage:.0f}%"
        )
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
    sub.add_parser("portfolio")
    rotation_backtest = sub.add_parser("rotation-backtest")
    rotation_backtest.add_argument("--days", type=int, default=30)
    sub.add_parser("test-discord")
    sub.add_parser("diagnostics")
    validation = sub.add_parser("validate-bottoms")
    validation.add_argument("symbols", nargs="*", default=["PUMP", "RNBW", "TROLL", "ZORA"])
    validation.add_argument("--days", type=int, default=7)
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
        "portfolio": cmd_portfolio,
        "rotation-backtest": cmd_rotation_backtest,
        "test-discord": cmd_test_discord,
        "diagnostics": cmd_diagnostics,
        "validate-bottoms": cmd_validate_bottoms,
    }
    await commands[args.command](args, settings)


async def run_command(args: argparse.Namespace, settings: Settings) -> None:
    try:
        await dispatch(args, settings)
    finally:
        await close_db()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(run_command(args, settings))


if __name__ == "__main__":
    main()
