# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m sentinel.db.init      # Initialize DuckDB schema

# Run
make run                        # Full pipeline (Ingester → Scanner → Judge)
make run-api                    # FastAPI server on port 8000
make run-dashboard              # React dashboard on port 3000
python -m sentinel --with-api   # Full pipeline + API in one process

# Individual stages
make run-ingester
make run-scanner
make run-judge

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

Penumbra is a real-time intelligence pipeline that monitors Polymarket (a prediction market) for **informed flow** — trades likely driven by private information. It runs as a single async process with three stages connected by `asyncio.Queue` (single-writer to DuckDB, no contention).

```
Polymarket WebSocket → Ingester → DuckDB → Scanner → Judge → FastAPI → Dashboard
```

### Stage 1: Ingester (`sentinel/ingester/`)
Subscribes to Polymarket's CLOB WebSocket. Parses `Trade` and `BookEvent` models, batches writes to DuckDB (20 trades / 1s), and periodically syncs market metadata via REST.

### Stage 2: Scanner (`sentinel/scanner/`)
Polls DuckDB and scores trades on four dimensions (0–100 composite) plus two multipliers:

**Component scores:**
- **Volume anomaly × OFI** (0–40 pts): MAD-based modified Z-score (robust to fat tails) amplified by Order Flow Imbalance — the ratio of directional buy/sell volume. Pure volume without directional skew is penalised (×0.8); strongly aligned flow (|OFI| ≥ 0.7) matching the trade direction is boosted (×1.5). Grounded in Andersen & Bondarenko (2014) critique of VPIN and the Polymarket anatomy paper (arXiv 2025).
- **Price impact** (0–20 pts): `size_usd / liquidity_usd` — Kyle's lambda proxy. Normalised against stored market liquidity.
- **Wallet reputation** (0–20 pts): Historical win rate on resolved markets + market concentration bonus (up to +10 pts if ≥80% of recent wallet trades are on this single market — concentrated domain exposure is a key insider indicator).
- **Funding anomaly** (0–20 pts): Wallet age via Polygon RPC (Alchemy).

**Multipliers (applied to total after components):**
- **Time-to-resolution urgency**: Trades placed < 24 h before market end_date score ×1.4; 24–72 h ×1.2; 72 h+ ×1.0. Grounded in Kyle (1985) — informed traders trade closest to resolution.

Signals scoring ≥ 30 are forwarded to the Judge queue.

**Key data fields on Signal:** `ofi_score` (order flow imbalance −1→1), `hours_to_resolution` (int|None), `market_concentration` (fraction of wallet's recent trades on this market).

### Stage 3: Judge (`sentinel/judge/`)
Two-tier LLM classification via AWS Bedrock:
- **Tier 1 (Amazon Nova Lite)**: Binary INFORMED/NOISE with confidence — ~5,000 calls/day budget
- **Tier 2 (Amazon Nova Pro)**: Optional deep reasoning for high-confidence signals (disabled by default)

News context (Tavily Search) is fetched and cached 12 hours for signals ≥ 70. `budget.py` enforces daily call limits tracked in DuckDB's `llm_budget` table.

### API (`sentinel/api/`)
FastAPI app on port 8000. Key routes: `/api/signals`, `/api/signals/stats`, `/api/markets`, `/api/wallets`, `/api/budget`, `/api/metrics`. In production, also serves the built React dashboard from `dashboard/dist`.

### Dashboard (`dashboard/`)
Vite + React + TypeScript app on port 3000 (dev). Uses React Query for data fetching, Recharts for charts, Tailwind CSS for styling. Four pages: Feed, MarketView, WalletView, Metrics.

### Database (`sentinel/db/`)
DuckDB (local OLAP, in-process). Core tables: `markets`, `trades`, `signals`, `signal_reasoning`, `llm_budget`, `book_snapshots`. Views: `v_hourly_volume`, `v_volume_anomalies`, `v_volume_anomalies_5m`, `v_5m_volume`, `v_order_flow_imbalance`, `v_coordination_signals`, `v_wallet_performance`.

## Key Configuration

`sentinel/config.py` is a Pydantic `BaseSettings` class — all environment variables are defined here. See `.env.example` for the full list. Required external services: AWS Bedrock (credentials), Alchemy (Polygon RPC), Tavily (news search).

## Testing

- Test markers: `@pytest.mark.integration` (needs live APIs), `@pytest.mark.slow` (>10s)
- Mocking: `moto[bedrock]` for AWS Bedrock, `respx` for httpx HTTP calls
- `make test` runs unit tests only (excludes `integration` marker)

## Code Style

Ruff with 100-char line length, strict rules (E, F, I, N, UP, B, SIM, T20, RUF). Mypy in strict mode (`disallow_untyped_defs`). Run `make check` before committing.
