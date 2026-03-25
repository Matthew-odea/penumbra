# Penumbra

> Real-time intelligence agent that monitors Polymarket for **Informed Flow** — trades likely driven by private information — and surfaces them on an analytical dashboard.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│              Vite + React Dashboard  (:3000)                 │
│          (Tailwind · Recharts · React Query)                 │
└──────────────┬──────────────────────────────────────────────┘
               │  REST  (proxied via Vite → :8000)
               ▼
┌──────────────────────────┐
│       FastAPI Gateway     │
│  (serves dashboard data)  │
└──────┬───────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────┐
│                    Python Core Engine                         │
│                                                               │
│  ┌─────────┐  ┌──────────┐  ┌────────────┐                  │
│  │ Ingester │→ │ Scanner  │→ │   Judge    │                  │
│  │ (Sprint1)│  │(Sprint 2)│  │ (Sprint 3) │                  │
│  └─────────┘  └──────────┘  └────────────┘                  │
│       ↕              ↕             ↕                          │
│  ┌──────────────────────────────────────────────────────┐    │
│  │              DuckDB (Local OLAP)                      │    │
│  │  markets · trades · signals · signal_reasoning        │    │
│  │  llm_budget · v_hourly_volume · v_wallet_performance  │    │
│  └──────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                      ↕                ↕
           ┌──────────────┐  ┌──────────────────┐
           │ Polygon RPC  │  │   AWS Bedrock    │
           │(funding chk) │  │ (Nova Lite/Pro)  │
           └──────────────┘  └──────────────────┘
                      ↕
           ┌──────────────────────┐
           │  Tavily / Exa Search │
           │  (news context)      │
           └──────────────────────┘
```

## Triple-Filter Pipeline

| # | Filter | Engine | Purpose |
|---|--------|--------|---------|
| 1 | **Statistical** | DuckDB SQL | Volume Z-score > 3σ, price-impact in illiquid markets |
| 2 | **Behavioral** | DuckDB + Polygon | Wallet win-rate history, fresh-wallet funding anomalies |
| 3 | **Intelligence** | AWS Bedrock + Search API | LLM cross-references trade against live news; scores 1-100 |

## Repository Layout

```
penumbra/
├── docs/                    # Architecture, integration, and sprint docs
│   ├── architecture/        # ADRs (DuckDB, Z-Score, Budget, Pipeline)
│   ├── integrations/        # Per-service integration guides
│   └── sprints/             # Sprint specs (0-4)
├── sentinel/                # Python core engine
│   ├── ingester/            # WebSocket listener & DuckDB writer
│   ├── scanner/             # Statistical signal detection
│   ├── judge/               # Bedrock LLM reasoning layer
│   ├── alerts/              # Alert delivery (TBD)
│   ├── api/                 # FastAPI gateway
│   ├── db/                  # DuckDB schema & helpers
│   └── config.py            # Centralized settings (Pydantic BaseSettings)
├── dashboard/               # Sprint 4 — Vite + React + Tailwind frontend
├── scripts/                 # One-off utilities (backfill, seed, setup)
├── tests/                   # Mirrors sentinel/ structure (229+ tests)
├── docker-compose.yml       # Local dev stack
├── pyproject.toml           # Python project config
├── .env.example             # Required environment variables
└── Makefile                 # Common dev commands
```

## Quick Start

```bash
# 1. Clone and install Python deps
git clone <repo-url> && cd penumbra
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Copy env template and fill in secrets
cp .env.example .env

# 3. Initialize DuckDB schema
python -m sentinel.db.init

# 4. Run API server
make run-api

# 5. Start the dashboard
cd dashboard && npm install && npm run dev
```

## Key Decisions

See [docs/architecture/](docs/architecture/) for full ADRs. Highlights:

- **DuckDB over ClickHouse/TimescaleDB**: Zero-ops, in-process OLAP. Perfect for a solo analyst running on a laptop or $5 VPS.
- **Modified Z-Score**: MAD (Median Absolute Deviation) instead of standard deviation to handle fat-tailed volume distributions.
- **Parallel LLM processing**: 8-worker pool processes ~13,824 signals/day (up from ~1,800 sequential). Nova Lite-only mode: 5,000 calls/day (~$0.50/day).
- **Smart news fetching**: 12-hour cache + only fetch for top signals (≥70) to stay within Tavily free tier (33 calls/day).
- **Single-writer pipeline**: Ingester → Scanner → Judge share one DuckDB connection via asyncio queues.

## Alerts

> **TODO:** Decide on an alert delivery mechanism for high-suspicion signals (score ≥ 80). Options to evaluate:
> - Telegram bot, Slack webhook, Discord webhook, email, or plain webhook
>
> For now, the pipeline stores all signals in DuckDB and surfaces them on the dashboard. Alert delivery is stubbed in `sentinel/alerts/`.

## Sprints

| Sprint | Name | Status | Docs |
|--------|------|--------|------|
| 0 | **Foundation** | ✅ Complete | [Sprint 0](docs/sprints/sprint-0-foundation.md) |
| 1 | **The Hose** | ✅ Complete | [Sprint 1](docs/sprints/sprint-1-hose.md) |
| 2 | **The Scanner** | ✅ Complete | [Sprint 2](docs/sprints/sprint-2-scanner.md) |
| 3 | **The Judge** | ✅ Complete | [Sprint 3](docs/sprints/sprint-3-judge.md) |
| 4 | **The Sentinel** | ✅ Complete | [Sprint 4](docs/sprints/sprint-4-sentinel.md) |

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Pipeline | Python 3.13, asyncio, structlog |
| Database | DuckDB 1.5 (6 tables, 7 views) |
| LLM | AWS Bedrock — Amazon Nova Lite (T1), Nova Pro (T2) |
| Funding | Polygon RPC via Alchemy |
| News | Tavily Search API |
| API | FastAPI + httpx |
| Frontend | Vite, React 18, TypeScript, Tailwind CSS, Recharts, React Query |
| Testing | pytest (229+ tests), pytest-asyncio, httpx AsyncClient |

## License

Private — not for redistribution.