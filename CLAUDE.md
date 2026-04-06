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
Polygon Chain (OrdersMatched) ──┐
Polymarket WS (last_trade_price) ──┼──> Ingester ──> DuckDB ──> Scanner ──> FastAPI ──> Dashboard
Polymarket REST (data-api/trades) ─┘
```

### Stage 1: Ingester (`sentinel/ingester/`)

Three data sources feed trades into DuckDB:

- **On-chain poller** (`chain_poller.py`): Polls Polygon `eth_getLogs` every 10s for `OrdersMatched` events from both Polymarket CTF Exchange contracts. Decodes taker wallet, token_id, side, and size via `eth_abi`. Provides wallet-attributed trades within ~10-12 seconds. Uses Alchemy RPC (free tier: ~648k CU/day, fits in 30M CU/month). This is the primary real-time trade source with full wallet data.
- **WebSocket** (`listener.py`): Subscribes to Polymarket's CLOB WS for `last_trade_price` events (fastest price signal, but **no wallet address**) and `price_changes` (order book). Subscriptions refresh every 30 min as the hot tier updates. 500 markets, ~1,000 token IDs.
- **REST poller** (`poller.py`): Polls `data-api.polymarket.com/trades` for the top 100 hot-tier markets every 5s. Uses a bounded LRU set (500K) for dedup. CDN-cached for 5 minutes, so trades arrive in bursts. Serves as backfill and provides wallet data for markets not yet chain-mapped.

Trades are batched by `writer.py` (20 trades or 1s, whichever comes first) and written to DuckDB. The `v_deduped_trades` view deduplicates across all three sources. Book events are used only for `book_snapshots` (liquidity cliff detection) — they do not enter the scanner queue.

**Market intelligence** (`market_scorer.py`): On first sync, all ~50,000 markets (via Gamma API) are scored for insider-trading attractiveness via Amazon Nova Lite (50,000/day budget). The priority formula `(attractiveness/100) x time_weight x uncertainty x liquidity_scaling` ranks markets into a hot tier of 100 (REST polling) / 500 (WS subscription).

Markets are fully synced every 2 hours. Markets absent from the API response are marked `active=false` automatically. Category-based exclusion uses an `IS NULL` guard (Gamma API returns null for category on most markets), supplemented by `scanner_min_attractiveness` score gating.

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

**Views:** `v_hourly_volume`, `v_volume_anomalies`, `v_volume_anomalies_5m`, `v_5m_volume`, `v_order_flow_imbalance`, `v_coordination_signals`, `v_wallet_performance`, `v_wallet_positions`, `v_signal_outcomes`, `v_deduped_trades`.

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
| `hot_market_count` | 100 | Size of hot REST polling tier |
| `ws_market_count` | 500 | WS subscription breadth |
| `ingester_batch_size` | 20 | Trades per write batch |
| `trade_poll_interval_seconds` | 5 | REST poller frequency |
| `chain_poll_interval_seconds` | 10 | On-chain poller frequency (10s free tier, 4s PAYG) |
| `chain_poll_enabled` | true | Enable/disable on-chain Polygon poller |
| `market_sync_interval_hours` | 2 | Full market metadata re-sync |
| `scanner_min_attractiveness` | 30 | Minimum attractiveness score to emit signals |

Required external services: AWS Bedrock (credentials via boto3 chain, for market attractiveness scoring only), Alchemy (Polygon RPC for on-chain trade polling and wallet funding checks).

## Testing

- 248+ tests across unit, regression, and integration suites
- Markers: `@pytest.mark.integration` (needs live APIs), `@pytest.mark.slow` (>10s)
- Mocking: `respx` for httpx HTTP calls
- `make test` runs unit tests only (excludes `integration` marker)
- Test fixtures use in-memory DuckDB with `init_schema()` — no external dependencies

## Code Style

Ruff with 100-char line length, strict rules (E, F, I, N, UP, B, SIM, T20, RUF). Mypy in strict mode (`disallow_untyped_defs`). Run `make check` before committing.
