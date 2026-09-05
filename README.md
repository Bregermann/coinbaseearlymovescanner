# Coinbase Early Move Scanner

A production-oriented, read-only scanner for detecting early high-upside moves across the Coinbase spot universe before they become obvious leaderboard gainers.

The scanner uses Coinbase Advanced Trade public market data as the source of truth for current price, trades, candles, order-book updates, and quote freshness. CoinGecko is used only for non-time-sensitive supply metadata, and live market-cap estimates are recalculated from the current Coinbase price when supply is available.

## What it does

- Discovers the full Coinbase USD/USDC spot universe at startup and refreshes it continuously.
- Excludes stablecoin/stablecoin markets and disabled/non-tradable products.
- Connects to Coinbase Advanced Trade WebSocket channels: `ticker`, `market_trades`, `candles`, `level2`, `status`, and `heartbeats`.
- Tracks exchange timestamp, ingestion timestamp, last trade timestamp, and quote age.
- Suppresses normal alerts when quote data is stale.
- Bootstraps and persists Coinbase candles so baselines survive restarts.
- Scores volume acceleration, turnover, compression, higher lows, base/resistance proximity, order-book conditions, microcap rotation, catalysts, and freshness.
- Posts polished Discord embeds only when 1-5 assets materially qualify.
- Deduplicates alerts and permits follow-ups for meaningful transitions such as breakout, invalidation, new catalyst, or status change.
- Stores alerts and scheduled observations for later performance analysis.

## Quick start

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -r requirements.txt
copy .env.example .env
python cli.py status
python cli.py test-discord
python cli.py run
```

For Linux/macOS shells, use `source .venv/bin/activate` instead of the Windows activation command.

## Required configuration

Only one secret is required for production alerts:

```env
DISCORD_ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

For production, use PostgreSQL:

```env
DATABASE_URL=postgresql+asyncpg://scanner:scanner_password@postgres:5432/scanner
```

SQLite is supported for local development:

```env
DATABASE_URL=sqlite+aiosqlite:///./scanner.db
```

## Docker deployment

```bash
copy .env.example .env
# edit .env and set DISCORD_ALERT_WEBHOOK_URL

docker compose up -d --build
```

The `scanner` service restarts automatically with `restart: unless-stopped`.

## CLI commands

```bash
python cli.py status
python cli.py scan
python cli.py coin RNBW
python cli.py microcaps
python cli.py catalysts
python cli.py performance
python cli.py test-discord
python cli.py run
```

`scan` prints ranked candidates without forcing Discord delivery. If nothing qualifies, no alert is sent.

## Freshness model

For each live market record the scanner stores:

- Coinbase exchange timestamp
- ingestion timestamp
- last trade timestamp when available
- quote age in seconds

Normal alerts are suppressed when `quote_age_seconds` exceeds `ALERT_MAX_QUOTE_AGE_SECONDS`. The Discord footer always includes:

```text
LIVE COINBASE DATA
Quote age: X.X sec
```

## Scoring philosophy

The scanner does not simply rank by 24h gain. Already extended assets are penalized unless a strong fresh catalyst exists. The strongest setups are generally:

- volume accelerating faster than price
- price near a base or just below resistance
- compressed recent range
- high volume/market-cap turnover
- thin but improving liquidity
- dormant microcap rotation
- fresh tier-1 catalyst affecting a Coinbase-listed asset

## Tests

```bash
pytest
```

## Important limitations

Catalyst scanners poll public official announcement pages. Some exchanges publish through dynamic pages or authenticated feeds; for fastest possible exchange announcements, add official project or exchange feed URLs to `CATALYST_SOURCE_URLS` when available.

This application is read-only. It never places trades and does not require Coinbase trading credentials.
