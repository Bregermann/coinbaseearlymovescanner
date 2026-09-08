# Coinbase Early Move Scanner

A production-oriented, read-only scanner for detecting early high-upside moves across the Coinbase spot universe before they become obvious leaderboard gainers.

The scanner uses Coinbase Advanced Trade public market data as the source of truth for current price, trades, candles, order-book updates, and quote freshness. CoinGecko is used only for non-time-sensitive supply metadata, and live market-cap estimates are recalculated from the current Coinbase price when supply is available.

## What it does

- Discovers the full Coinbase USD/USDC spot universe at startup and refreshes it continuously.
- Excludes stablecoin/stablecoin markets and disabled/non-tradable products.
- Connects to Coinbase Advanced Trade WebSocket channels: `ticker`, `market_trades`, `candles`, `level2`, `status`, and `heartbeats`.
- Tracks exchange timestamp, ingestion timestamp, last trade timestamp, quote age, and last Coinbase message.
- Suppresses normal alerts when quote data is stale.
- Bootstraps and persists Coinbase candles so baselines survive restarts.
- Scores volume acceleration, turnover, compression, higher lows, base/resistance proximity, order-book conditions, bottoming structures, microcap rotation, catalysts, and freshness.
- Fits swing highs/lows and channel boundaries across 15m, 1h, 4h, and 1d context to detect descending channels, bull flags, falling wedges, double/rounded bottoms, higher-low bases, failed breakdown reclaims, and breakout retests.
- Tracks BOTTOM FORMING, PRE-BREAKOUT, BREAKOUT FIRING, and RETEST HOLD; PRE-BREAKOUT is the preferred early alert.
- Detects volume dry-up followed by the first 15m/1h/4h expansion rather than waiting for already-obvious 24h volume.
- Selects a meaningful active impulse across 15m/1h/4h/1d closed candles and reports reliable Fibonacci retracements, extensions, confluence, and reclaim/bounce states.
- Optionally compares every candidate with explicitly configured holdings, protects relative-strength leaders, and recommends only conservative partial rotations when the advantage is material.
- Treats missing unique-trader/Gooner-style confirmation signals as `N/A` diagnostics, not hard alert blockers.
- Posts polished Discord embeds only when assets materially qualify.
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
python cli.py diagnostics
python cli.py run
```

For Linux/macOS shells, use `source .venv/bin/activate` instead of the Windows activation command.

## Required configuration

Only one secret is required for production alerts:

```env
DISCORD_ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_DEBUG_WEBHOOK_URL=
DISCORD_HEARTBEAT_SECONDS=3600
DISCORD_HEARTBEAT_ALLOW_PRIMARY=false
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

## Windows watchdog

When Docker is unavailable, the scripts in `scripts/` can run the scanner under a hidden watchdog process:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_watchdog.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\status_watchdog.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop_watchdog.ps1
```

`install_windows_task.ps1` attempts to create a logon Scheduled Task. If Windows denies that, a per-user Startup shortcut or another user-level launcher can call `start_watchdog.ps1`.

## CLI commands

```bash
python cli.py status
python cli.py scan
python cli.py diagnostics
python cli.py validate-bottoms
python cli.py coin RNBW
python cli.py microcaps
python cli.py catalysts
python cli.py performance
python cli.py portfolio
python cli.py rotation-backtest --days 30
python cli.py test-discord
python cli.py run
```

`scan` prints ranked qualifying candidates without forcing Discord delivery. If nothing qualifies, no alert is sent.

`diagnostics` evaluates the current Coinbase universe, prints top near-misses, suppression reasons, baseline completeness, recent alert counts, and Discord configuration status without exposing webhook secrets.

`validate-bottoms` performs a closed-candle, no-lookahead replay for PUMP, RNBW, TROLL, and ZORA by default and reports first alert time/price, selected breakout time/price, stage, scores, and maximum post-alert upside. Pass other symbols or use `--days` to change the recent breakout window.

`portfolio` prints current hold scores and `ROTATION_PROTECTED` states. `rotation-backtest` compares destination return with the source holding at 1h, 4h, 12h, 24h, 3d, and 7d. It reports an insufficient sample rather than inventing statistics.

If `DISCORD_DEBUG_WEBHOOK_URL` is set, the scanner sends an hourly `SCANNER ALIVE` heartbeat there. Without a debug webhook, the heartbeat is logged locally. It is not sent to the production alert channel unless `DISCORD_HEARTBEAT_ALLOW_PRIMARY=true` is explicitly configured.

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

The scanner does not simply rank by 24h gain. Already extended assets are penalized unless a strong fresh catalyst or a genuinely new consolidation exists. The strongest setups are generally:

- volume accelerating faster than price
- price near a base or just below resistance
- compressed recent range
- high volume/market-cap turnover
- thin but improving liquidity
- dormant microcap rotation
- fresh tier-1 catalyst affecting a Coinbase-listed asset
- a 15-50% reset from a recent local high followed by defended support
- contracting ATR/Bollinger width, fading sell pressure, and improving MACD/RSI/OBV
- price within roughly 1-8% of fitted descending or horizontal resistance
- volume dry-up followed by the first expansion bar

These are weighted signals, not universal mandatory gates. Missing unique-trader data, Gooner EMA confirmation, microcap metadata, catalysts, compression, and order-book imbalance can reduce confidence or appear as diagnostics, but they do not automatically disqualify an otherwise strong setup.

## Fibonacci engine

The Fib engine uses closed candles only. For each timeframe it finds pivot-low to later pivot-high impulses, then requires a minimum percentage move, at least 3.5 ATR of travel, a confirmed pivot, relevant age, and current-price relevance. It ranks eligible swings by magnitude (24%), ATR significance (20%), pivot volume expansion (15%), recency (16%), and alignment with the current base/structure (25%), with a small 1h/4h preference. Swings below `FIB_MIN_SWING_CONFIDENCE` display `Fib: N/A`.

Retracements are 0.236, 0.382, 0.500, 0.618, and 0.786. Extensions are 1.000, 1.272, 1.414, 1.618, 2.000, and 2.618. Confluence can come from detected support/resistance, base boundaries, local pivots, 20/50 EMA, recent VWAP, volume shelves, and round-number structure. A naked Fib starts with low confidence and never acts as a mandatory alert filter. Extension probabilities are empirical rates from completed prior impulse/retracement/reclaim samples; fewer than `FIB_PROBABILITY_MIN_SAMPLES` reports an insufficient sample.

## Portfolio rotation

No holdings are inferred. Copy `portfolio.example.json` to the ignored local file `portfolio.json`, then add only positions that actually exist. Each entry accepts `ticker` and `quantity`; `cost_basis` and `current_position_value` are optional. Cost basis is input context only and is deliberately excluded from rotation decisions. The equivalent `PORTFOLIO_HOLDINGS_JSON` environment variable can be used instead of a file.

The current hold score is:

```text
31% scanner score + 18% trend + 12% relative strength
+ 12% participation + 10% support integrity + 8% upside runway
+ 7% liquidity safety + 2% order book - 20% deterioration
```

The candidate comparison score is:

```text
68% risk-adjusted opportunity + 9% upside runway + 8% liquidity
+ 8% volume acceleration + 5% Fib confluence + 2% relative strength
- 18% of the extension penalty
```

Defaults require a 10-point edge for `WATCH_ROTATION`, 15 points for `PARTIAL_ROTATION`, and 25 points for `STRONG_ROTATION`. A partial/strong recommendation also requires candidate R/R >=1.5, R/R improvement >=0.75, holding deterioration >=35, confidence >=65, acceptable candidate extension, and a useful non-dust source position. Strong holdings above support with continuing participation receive `ROTATION_PROTECTED`.

Recommendations suggest 10-25% trims by default, capped at 35% for a strong rotation. They never suggest 100%. Pair cooldown is six hours; reverse-direction hysteresis is twelve hours and requires eight extra advantage points. Recommendations and relative outcomes are persisted in `rotation_recommendations` and `rotation_observations`.

## Tests

```bash
pytest
```

## Important limitations

Catalyst scanners poll public official announcement pages. Some exchanges publish through dynamic pages or authenticated feeds; for fastest possible exchange announcements, add official project or exchange feed URLs to `CATALYST_SOURCE_URLS` when available.

This application is read-only. It never places trades and does not require Coinbase trading credentials.
