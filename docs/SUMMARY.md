# Penumbra — Project Summary

> Consolidated reference for the Penumbra informed-flow detection system.  
> For details, see the individual docs linked in each section.

---

## What It Does

Penumbra monitors Polymarket prediction markets in real time, detects trades
that are likely driven by private information ("informed flow"), and surfaces
them on an analytical dashboard for review.

## Pipeline

```
Polymarket WebSocket
        │
        ▼
  ┌─────────┐     asyncio.Queue     ┌──────────┐     asyncio.Queue     ┌────────────┐
  │ Ingester │  ─────────────────▶  │ Scanner  │  ─────────────────▶  │   Judge    │
  │ (Sprint1)│    Trade batches     │(Sprint 2)│    Signal objects    │ (Sprint 3) │
  └─────────┘                       └──────────┘                       └────────────┘
       │                                  │                                  │
       └──────── DuckDB (read/write) ─────┴──────────────────────────────────┘
                                          │
                              ┌───────────┴───────────┐
                              ▼                       ▼
                     FastAPI Gateway         Dashboard (Vite + React)
                       :8000                    :3000
```

### Stage 1 — Ingester

- Connects to Polymarket CLOB WebSocket for live trades + order book events
- Batches writes to DuckDB `trades` table
- Syncs `markets` metadata periodically via REST

### Stage 2 — Scanner

Four detection layers, scored 0-100:

| Layer | Points | Source |
|-------|--------|--------|
| Volume anomaly (Modified Z-Score) | 0-40 | DuckDB `v_volume_anomalies` view |
| Price impact (ΔP / L × V) | 0-20 | DuckDB `trades` + `markets` |
| Wallet profiling (win-rate) | 0-20 | DuckDB `v_wallet_performance` view |
| Funding anomaly (wallet age) | 0-20 | Polygon RPC via Alchemy |

Trades scoring ≥ 30 become `Signal` objects forwarded to the Judge.

### Stage 3 — Judge

Two-tier LLM reasoning via AWS Bedrock:

| Tier | Model | Budget | Purpose |
|------|-------|--------|---------|
| T1 | Amazon Nova Lite | 200/day | Quick classify: INFORMED vs NOISE + confidence |
| T2 | Amazon Nova Pro | 30/day | Deep reasoning for T1 confidence ≥ 60 |

Also fetches news headlines via Tavily Search for context.  
Results stored in `signal_reasoning` table.  
Worst-case daily cost: **~$0.05**.

### Stage 4 — Dashboard

Vite + React + TypeScript + Tailwind CSS frontend with three pages:

- **Feed** — Summary cards, score filter, signal table with expandable reasoning
- **Market drill-down** — Volume chart (Recharts), market signals
- **Wallet profiler** — Win rate, category breakdown, trade history

Data served by FastAPI gateway from DuckDB.

## Database Schema (DuckDB)

| Table | Purpose |
|-------|---------|
| `markets` | Polymarket market metadata (question, category, liquidity) |
| `trades` | Raw trade events from WebSocket |
| `signals` | Scored signals from the scanner (statistical_score 0-100) |
| `signal_reasoning` | LLM classification + reasoning from the judge |
| `llm_budget` | Daily call tracking per tier |

Views: `v_hourly_volume`, `v_volume_anomalies`, `v_wallet_performance`

## External Services

| Service | Purpose | Config Key |
|---------|---------|------------|
| AWS Bedrock | LLM inference (Nova Lite + Pro) | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |
| Alchemy | Polygon RPC for wallet funding checks | `ALCHEMY_API_KEY` |
| Tavily | News search for market context | `TAVILY_API_KEY` |
| Polymarket | Trade data source (WebSocket + REST) | Built-in URLs |

## Architecture Decisions

| ADR | Decision | Rationale |
|-----|----------|-----------|
| [001](docs/architecture/adr-001-duckdb.md) | DuckDB as local OLAP | Zero-ops, in-process, perfect for single analyst |
| [002](docs/architecture/adr-002-modified-zscore.md) | Modified Z-Score | Robust to fat-tailed volume distributions |
| [003](docs/architecture/adr-003-bedrock-budget.md) | Two-tier budget cap | Hard caps prevent runaway LLM costs |
| [004](docs/architecture/adr-004-pipeline-architecture.md) | Single-writer pipeline | Asyncio queues, no multi-process complexity |

## Test Suite

229+ tests across 29 files:

| Module | Tests | Coverage |
|--------|-------|----------|
| Ingester | ~53 | Backfill, parsing, batch writing |
| Scanner | ~62 | Volume, price impact, wallet profiling, funding, pipeline |
| Judge | ~90 | Budget, classifier, news, prompts, reasoner, pipeline, store |
| API | 25 | Health, signals, markets, wallets, budget endpoints |
| Config | ~4 | Settings loading |

```bash
make test     # Run all unit tests
```

## Sprint History

| Sprint | Name | What was built |
|--------|------|---------------|
| 0 | Foundation | Repo, docs, config, DuckDB schema, integration research |
| 1 | The Hose | WebSocket ingester, trade parser, batch writer |
| 2 | The Scanner | Z-score, price impact, wallet profiler, funding check, composite scorer |
| 3 | The Judge | Bedrock classifier + reasoner, budget manager, news fetcher, signal store |
| 4 | The Sentinel | FastAPI gateway (5 route modules), Vite+React dashboard (3 pages) |
