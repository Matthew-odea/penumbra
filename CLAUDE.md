# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m sentinel.db.init      # Initialize DuckDB schema

# Run
make run                        # Full pipeline (Ingester + Scanner)
make run-api                    # FastAPI server on port 8000
make run-dashboard              # React dashboard on port 3000
python -m sentinel --with-api   # Full pipeline + API in one process

# Individual stages
make run-ingester
make run-scanner

# Test
make test                       # Unit tests with coverage (excludes integration)
make test-all                   # All tests including integration (requires API keys)
pytest tests/scanner/ -k "test_scorer"   # Run a single test or module

# Quality
make lint                       # ruff check
make format                     # ruff format + fix
make typecheck                  # mypy
make check                      # lint + typecheck + test
```

## Architecture

Penumbra is a real-time intelligence pipeline that monitors Polymarket (a prediction market) for **informed flow** — trades likely driven by private information. It runs as a single async process with two stages connected by `asyncio.Queue` (single-writer to DuckDB, no contention).

```
Polymarket WS + REST --> Ingester --> DuckDB --> Scanner --> FastAPI --> Dashboard
```

### Stage 1: Ingester (`sentinel/ingester/`)

Two data sources feed trades into DuckDB:

- **WebSocket** (`listener.py`): Subscribes to Polymarket's CLOB WS for `last_trade_price` events and `price_changes` (order book). Subscriptions refresh every 30 min as the hot tier updates.
- **REST poller** (`poller.py`): Polls `data-api.polymarket.com/trades` for the top 50 hot-tier markets every 5s. Uses a bounded LRU set (200K) for dedup.

Trades are batched by `writer.py` (20 trades or 1s, whichever comes first) and written to DuckDB. Book events are used only for `book_snapshots` (liquidity cliff detection) — they do not enter the scanner queue.

**Market intelligence** (`market_scorer.py`): On first sync, all ~3900 markets are scored for insider-trading attractiveness via Amazon Nova Lite (separate 4,000/day budget). The priority formula `(attractiveness/100) x time_weight x uncertainty x liquidity_scaling` ranks markets into a hot tier of 50 that gets intensive polling.

Markets are fully synced every 2 hours. Markets absent from the API response are marked `active=false` automatically.

### Stage 2: Scanner (`sentinel/scanner/`)

Scores each trade on four base components (sum to 100 max) plus multiplicative boosters:

**Base components:**
- **Volume anomaly x OFI** (0-40 pts): MAD-based modified Z-score amplified by Order Flow Imbalance. Aligned directional flow (|OFI| >= 0.7) boosts x1.5; balanced flow penalises x0.8.
- **Price impact** (0-20 pts): `|deltaP| / liquidity x size_usd`. Falls back to $10K liquidity when Polymarket returns null.
- **Wallet reputation** (0-20 pts): Win rate on resolved markets + concentration bonus (+5-10 pts if >= 50-80% of wallet's recent trades target this single market).
- **Funding anomaly** (0-20 pts): Wallet age via Alchemy RPC. Tiered decay: 20 pts (<15 min) down to 0 pts (>72h).

**Multipliers (applied after base, score is uncapped):**
- **Time-to-resolution**: x1.4 (<24h), x1.2 (24-72h)
- **Liquidity cliff**: x1.2 if spread widened >30% in last 10 min
- **Coordination**: x1.15-1.3 if >= 3 distinct wallets traded same side in 5-min window

Signals scoring >= 30 are persisted to DuckDB. High-scoring signals (>= 80) get a template-based natural language explanation generated at read time (no LLM needed).

### API (`sentinel/api/`)

FastAPI on port 8000. Routes: `/api/signals`, `/api/signals/stats`, `/api/markets`, `/api/markets/{id}`, `/api/markets/{id}/volume`, `/api/markets/{id}/anomalies`, `/api/watchlist`, `/api/wallets`, `/api/wallets/{addr}`, `/api/budget`, `/api/metrics/timeseries`, `/api/metrics/overview`, `/api/metrics/accuracy`, `/api/metrics/patterns`, `/api/metrics/ingestion`, `/api/health`. In production, serves the built React dashboard from `dashboard/dist`.

### Dashboard (`dashboard/`)

Vite + React + TypeScript on port 3000 (dev). React Query for data fetching, Recharts for charts, Tailwind CSS with dark theme. Seven pages: Feed (signal table with filters), Watchlist (hot-tier priority ranking), Markets (all markets by tier), MarketView (detail + volume chart), Wallets (smart money leaderboard), WalletView (profile + trades + signals), Metrics (pipeline analytics + accuracy + patterns).

### Database (`sentinel/db/`)

DuckDB (local OLAP, in-process). Single-writer architecture.

**Tables:** `markets` (with `token_ids`, `attractiveness_score`), `trades`, `signals`, `signal_reasoning` (deprecated), `llm_budget`, `book_snapshots`, `vpin_buckets`, `market_lambda`.

**Views:** `v_hourly_volume`, `v_volume_anomalies`, `v_volume_anomalies_5m`, `v_5m_volume`, `v_order_flow_imbalance`, `v_coordination_signals`, `v_wallet_performance`, `v_signal_outcomes`, `v_deduped_trades`.

Migrations (v002-v014) are applied idempotently in `init_schema()`.

## Key Configuration

`sentinel/config.py` is a Pydantic `BaseSettings` singleton. All values are loaded from environment variables or `.env`. Key defaults:

| Setting | Default | Purpose |
|---------|---------|---------|
| `zscore_threshold` | 2.0 | Modified Z-score threshold for volume anomaly |
| `min_trade_size_usd` | 100 | Minimum trade size to process |
| `signal_min_score` | 30 | Minimum composite score to emit signal |
| `alert_min_score` | 80 | Minimum score for alert emission |
| `bedrock_market_scoring_daily_limit` | 50000 | Market attractiveness scoring budget |
| `hot_market_count` | 100 | Size of hot polling tier |
| `ingester_batch_size` | 20 | Trades per write batch |
| `trade_poll_interval_seconds` | 5 | REST poller frequency |
| `market_sync_interval_hours` | 2 | Full market metadata re-sync |

Required external services: AWS Bedrock (credentials via boto3 chain, for market attractiveness scoring only), Alchemy (Polygon RPC).

## Testing

- 230+ tests across unit, regression, and integration suites
- Markers: `@pytest.mark.integration` (needs live APIs), `@pytest.mark.slow` (>10s)
- Mocking: `respx` for httpx HTTP calls
- `make test` runs unit tests only (excludes `integration` marker)
- Test fixtures use in-memory DuckDB with `init_schema()` — no external dependencies

## Code Style

Ruff with 100-char line length, strict rules (E, F, I, N, UP, B, SIM, T20, RUF). Mypy in strict mode (`disallow_untyped_defs`). Run `make check` before committing.
