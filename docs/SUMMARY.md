# Penumbra

Real-time informed-flow detection for Polymarket prediction markets.

Penumbra monitors trades on Polymarket, scores them for statistical anomalies and behavioral signals, classifies them via LLM reasoning, and surfaces suspicious activity on an analytical dashboard.

---

## Quick Start

```bash
# 1. Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                # Fill in API keys

# 2. Initialize database
python -m sentinel.db.init

# 3. Run
make run                            # Full pipeline
# or
python -m sentinel --with-api       # Pipeline + API server
```

Dashboard (separate terminal):
```bash
cd dashboard && npm install && npm run dev
```

---

## Architecture

```
Polymarket WS ----+
                  |
                  v
            +-----------+    asyncio.Queue    +----------+    asyncio.Queue    +---------+
            | Ingester  | -----------------> | Scanner  | -----------------> |  Judge  |
            +-----------+    Trade batches   +----------+    Signal objects   +---------+
                  |                               |                               |
Polymarket REST --+                               |                               |
                  |                               |                               |
                  +----------- DuckDB (single writer) ---------------------------+
                                                  |
                                    +-------------+-------------+
                                    |                           |
                              FastAPI :8000              Dashboard :3000
```

The system runs as a **single async process**. Three pipeline stages communicate via `asyncio.Queue`. DuckDB is the sole data store, accessed by a single writer (the ingester) to avoid contention.

### Stage 1: Ingester

**Location:** `sentinel/ingester/`

Captures data from two sources:

| Source | What | Frequency | Data |
|--------|------|-----------|------|
| WebSocket (`listener.py`) | `last_trade_price` events + `price_changes` (order book) | Real-time | Trades (no wallet address), book snapshots |
| REST poller (`poller.py`) | `data-api.polymarket.com/trades` | Every 5s | Trades with wallet address, tx hash |

The **WS listener** subscribes to token_ids for the current hot-tier markets. Subscriptions refresh every 30 minutes when the hot tier updates, so newly prioritised markets start receiving WS events without a restart.

The **REST poller** polls the top 50 hot-tier markets in parallel (chunks of 10, 200ms between chunks). Uses a bounded LRU set (200K entries) for trade dedup.

**Batch writer** (`writer.py`) buffers trades and flushes to DuckDB when 20 trades accumulate or 1 second passes, whichever comes first. Uses `INSERT OR IGNORE` for dedup on `trade_id`.

**Market intelligence:** On startup, all active Polymarket markets (~3900) are synced and queued for LLM attractiveness scoring via Amazon Nova Lite. Each market gets a 0-100 score reflecting how likely it can be insider-traded (e.g., 90 for "Will Trump bomb Iran?" vs 15 for "Will BTC close above $64K?"). Markets are re-synced every 2 hours; markets absent from the API response are automatically deactivated.

The **hot tier** of 50 markets is selected by a priority formula:

```
priority = (attractiveness/100) x time_weight x uncertainty x liquidity_scaling
```

Where:
- `time_weight`: 1.0 (<1 day to resolution) ... 0.1 (>180 days)
- `uncertainty`: `1 - |price - 0.5| x 2` (markets near 50/50 are most interesting)
- `liquidity_scaling`: `min(liquidity, $500K) / $500K`

### Stage 2: Scanner

**Location:** `sentinel/scanner/`

Processes each trade through four detection layers that produce a composite score:

| Layer | Points | Source | What it detects |
|-------|--------|--------|-----------------|
| Volume anomaly x OFI | 0-40 | `v_volume_anomalies`, `v_volume_anomalies_5m` | Unusual volume spikes with directional flow |
| Price impact | 0-20 | `trades` + `markets` | Trades that moved the price relative to liquidity |
| Wallet reputation | 0-20 | `v_wallet_performance` | Historically accurate traders + market concentration |
| Funding anomaly | 0-20 | Alchemy RPC | Recently funded wallets (< 72h) |

**Multipliers** (applied to the total; the score is **not capped at 100**):

| Multiplier | Condition | Factor |
|------------|-----------|--------|
| Time-to-resolution | < 24h before market end_date | x1.4 |
| Time-to-resolution | 24-72h before end_date | x1.2 |
| Liquidity cliff | Spread widened >30% in last 10 min | x1.2 |
| Coordination | >= 3 wallets, same side, 5-min window | x1.15-1.3 |

Signals scoring >= 30 (`signal_min_score`) are written to DuckDB and forwarded to the Judge.

**Key design decisions:**
- Volume Z-score uses MAD (median absolute deviation) instead of standard deviation to be robust against fat-tailed distributions common in prediction markets.
- OFI (Order Flow Imbalance) amplifies or dampens the volume signal: aligned directional flow x1.5, balanced flow x0.8.
- Scores are uncapped so multipliers preserve relative ranking for the Judge. A score of 140 is meaningfully different from 100.

### Stage 3: Judge

**Location:** `sentinel/judge/`

LLM-powered classification via AWS Bedrock with an 8-worker parallel pool:

| Tier | Model | Budget | Trigger |
|------|-------|--------|---------|
| Tier 1 | Amazon Nova Lite | 5,000/day | All signals |
| Tier 2 | Amazon Nova Pro | 0/day (disabled) | T1 confidence >= 60 |

**Flow per signal:**
1. Budget check (atomic `UPDATE ... WHERE calls_used < calls_limit`)
2. Market context lookup (question, category, liquidity)
3. News fetch (Tavily, Exa fallback) for signals scoring >= 70, cached 12h
4. Tier 1 classification: INFORMED or NOISE with confidence 0-100
5. Optional Tier 2 deep reasoning (if enabled and T1 confidence >= 60)
6. Store reasoning to `signal_reasoning` table
7. Emit alert if final score >= 80

**Budget isolation:** Market attractiveness scoring uses a separate 4,000/day budget pool (`market_scoring` tier) so it cannot starve the judge's 5,000/day tier1 pool.

### API

**Location:** `sentinel/api/`

FastAPI on port 8000. In production, also serves the React dashboard from `dashboard/dist`.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/signals` | Paginated signal feed with filters (score, market, wallet) |
| `GET /api/signals/stats` | Today's counts (total, high suspicion, active markets) |
| `GET /api/markets` | All markets with tier classification and sorting |
| `GET /api/watchlist` | Hot-tier markets ranked by priority formula |
| `GET /api/markets/{id}` | Market detail with attractiveness score |
| `GET /api/markets/{id}/volume` | Hourly volume data for charting |
| `GET /api/markets/{id}/anomalies` | Volume Z-scores (last 24h) |
| `GET /api/markets/{id}/signals` | Signals for a specific market |
| `GET /api/wallets` | Smart money leaderboard (win rate x resolved trades) |
| `GET /api/wallets/{addr}` | Wallet profile (trades, win rate, category breakdown) |
| `GET /api/wallets/{addr}/trades` | Trade history |
| `GET /api/wallets/{addr}/signals` | Signals from a wallet |
| `GET /api/budget` | LLM call limits and usage today |
| `GET /api/metrics/timeseries` | Bucketed pipeline activity |
| `GET /api/metrics/overview` | Funnel, classification, score distribution |
| `GET /api/metrics/ingestion` | Trade ingestion stats |
| `GET /api/metrics/accuracy` | Classification accuracy on resolved markets |
| `GET /api/metrics/patterns` | Hour-of-day trading patterns |
| `GET /api/health` | Pipeline health check |

### Dashboard

**Location:** `dashboard/`

Vite + React 18 + TypeScript. Tailwind CSS dark theme. React Query for auto-refreshing data (10s-60s intervals depending on endpoint). Recharts for visualisation.

| Page | Route | What it shows |
|------|-------|---------------|
| Feed | `/` | Signal table with score filter buttons, expandable rows with reasoning |
| Watchlist | `/watchlist` | Hot-tier markets ranked by priority score, urgency color coding |
| Markets | `/markets` | All markets with tab filters (Hot / Scored / Pending), search |
| Market Detail | `/market/:id` | Volume chart, anomaly overlay, market signals |
| Wallets | `/wallets` | Smart money leaderboard ranked by win rate |
| Wallet Detail | `/wallet/:addr` | Trade history with WIN/LOSS outcomes, category breakdown |
| Metrics | `/metrics` | Pipeline activity, detection funnel, accuracy, budget usage |

---

## Database Schema

DuckDB (in-process OLAP). Schema defined in `sentinel/db/init.py`.

### Tables

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `markets` | Polymarket metadata | `market_id` (PK), `question`, `end_date`, `liquidity_usd`, `attractiveness_score`, `token_ids` |
| `trades` | Raw trade events | `trade_id` (PK), `market_id`, `wallet`, `side`, `price`, `size_usd`, `timestamp`, `source` |
| `signals` | Scored signals | `signal_id` (PK), `statistical_score`, `ofi_score`, `hours_to_resolution`, `market_concentration`, `coordination_wallet_count`, `liquidity_cliff` |
| `signal_reasoning` | LLM classification | `signal_id` (PK), `classification`, `suspicion_score`, `reasoning`, `key_evidence`, `news_headlines` |
| `llm_budget` | Daily call tracking | `(date, tier)` PK, `calls_used`, `calls_limit` |
| `book_snapshots` | Order book snapshots | `(market_id, ts)` PK, `best_bid`, `best_ask` |

### Views

| View | Purpose |
|------|---------|
| `v_hourly_volume` | Hourly trade aggregates (last 7 days) |
| `v_volume_anomalies` | Modified Z-score on hourly buckets (last 24h) |
| `v_volume_anomalies_5m` | Modified Z-score on 5-min buckets (last 2h) |
| `v_5m_volume` | 5-minute trade aggregates (last 2h) |
| `v_order_flow_imbalance` | OFI per market-hour (last 24h) |
| `v_coordination_signals` | >= 3 wallets, same side, 5-min window (last 10 min) |
| `v_wallet_performance` | Win rate on resolved markets (>= 5 trades, excludes empty wallets) |

Migrations (v002-v010) are applied idempotently during `init_schema()`. A `CHECKPOINT` is forced after migrations to prevent WAL corruption on crash.

---

## External Services

| Service | Purpose | Required? | Config |
|---------|---------|-----------|--------|
| AWS Bedrock | LLM inference (Nova Lite + Nova Pro) | Yes | boto3 credential chain |
| Alchemy | Polygon RPC for wallet funding age | Yes | `ALCHEMY_API_KEY` |
| Tavily | News search for market context | Yes | `TAVILY_API_KEY` |
| Exa | Fallback news search | No | `EXA_API_KEY` |
| Polymarket | Trade data (WS + REST) | Yes | Built-in URLs (no auth needed for public data) |

---

## Configuration

All configuration lives in `sentinel/config.py` as a Pydantic `BaseSettings` class. Values are loaded from environment variables or `.env`. See `.env.example` for the complete list with defaults and descriptions.

Key tuning parameters:

| Parameter | Default | Impact |
|-----------|---------|--------|
| `ZSCORE_THRESHOLD` | 2.0 | Lower = more signals, higher = fewer false positives |
| `MIN_TRADE_SIZE_USD` | 100 | Minimum trade size to analyze |
| `SIGNAL_MIN_SCORE` | 30 | Gate for forwarding to Judge (lower = more LLM calls) |
| `HOT_MARKET_COUNT` | 50 | Number of markets in the intensive polling tier |
| `SCORER_WEIGHT_*` | 40/20/20/20 | Relative importance of detection layers (must sum to 100) |

---

## Project Structure

```
sentinel/
  config.py              # Pydantic settings (single source of truth for all config)
  auth.py                # Polymarket L2 authentication
  __main__.py            # Top-level orchestrator (--with-api flag)
  ingester/
    __main__.py          # Pipeline entry point, background tasks, status logging
    listener.py          # WebSocket consumer (last_trade_price + price_changes)
    poller.py            # REST trade poller (hot tier, bounded dedup)
    writer.py            # Batch writer to DuckDB (20 trades / 1s flush)
    markets.py           # Market sync, upsert (CSV bulk load), priority formula
    market_scorer.py     # LLM attractiveness scoring (Nova Lite, 4K/day budget)
    models.py            # Trade, BookEvent dataclasses + parsers
  scanner/
    pipeline.py          # Per-trade processing through 4 detection layers
    scorer.py            # Composite scoring formula + Signal dataclass
    volume.py            # Z-score, OFI, concentration, coordination, liquidity cliff
    price_impact.py      # Price movement relative to liquidity
    wallet_profiler.py   # Win rate on resolved markets
    funding.py           # Wallet age via Alchemy RPC
  judge/
    pipeline.py          # 8-worker parallel LLM processing
    classifier.py        # Tier 1 — INFORMED/NOISE classification (Nova Lite)
    reasoner.py          # Tier 2 — Deep reasoning (Nova Pro, disabled by default)
    budget.py            # Daily call tracking with atomic check-and-increment
    news.py              # Tavily + Exa news fetching with LRU cache
    store.py             # Signal reasoning persistence + alert construction
  api/
    main.py              # FastAPI app with CORS, SPA fallback
    deps.py              # DuckDB connection singleton
    routes/              # health, signals, markets, wallets, budget, metrics
  db/
    init.py              # Schema DDL, views, migrations (v002-v010)
dashboard/
  src/
    api/client.ts        # API client (23+ fetch functions)
    api/types.ts         # TypeScript interfaces matching API responses
    pages/               # Feed, Watchlist, Markets, MarketView, Wallets, WalletView, Metrics
    components/          # Layout, SignalTable, SummaryCards, VolumeChart
    hooks/queries.ts     # React Query hooks with auto-refresh
    lib/format.ts        # Display utilities (USD, addresses, relative time, colors)
docs/
  architecture/          # ADRs (DuckDB, Z-score, Bedrock budget, pipeline)
  integrations/          # Polymarket, AWS Bedrock, Tavily, Alchemy, Supabase
  sprints/               # Historical sprint documentation (0-4)
  DEPLOYMENT.md          # AWS/Terraform deployment guide
  AUDIT.md               # Code audit findings
tests/                   # 325+ tests (unit, regression, integration)
```

---

## Testing

```bash
make test                # Unit tests only (325+ tests, ~14s)
make test-all            # Including integration tests (requires live API keys)
make test-integration    # Integration tests only
```

- **Mocking:** `moto[bedrock]` for AWS, `respx` for httpx
- **Markers:** `@pytest.mark.integration`, `@pytest.mark.slow`
- **Fixtures:** In-memory DuckDB via `init_schema()`, seeded test data in `tests/api/conftest.py`

---

## Deployment

Production runs on AWS EC2 via Docker. See `docs/DEPLOYMENT.md` for the full Terraform + GitHub Actions setup.

```bash
# Operational commands
make logs               # Last 100 CloudWatch log lines
make logs-live          # Tail live logs
make ssm                # SSH-free shell on EC2
make fix-wal            # DuckDB WAL corruption recovery
```
