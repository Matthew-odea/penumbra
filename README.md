# Penumbra

> Real-time intelligence agent that monitors Polymarket for **Informed Flow** — trades likely driven by private information — and surfaces them on an analytical dashboard.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                      Next.js Dashboard                       │
│              (Tremor v3 · Vercel / self-hosted)              │
└──────────────┬──────────────────────────────────────────────┘
               │  REST / Realtime
               ▼
┌──────────────────────────┐
│       FastAPI Gateway     │
│  (serves dashboard data)  │
└──────┬──────────┬────────┘
       │          │
       ▼          ▼
┌───────────┐ ┌─────────────────────────────────────────────┐
│  Supabase │ │             Python Core Engine               │
│ (Postgres)│ │                                               │
│  - Wallet │ │  ┌─────────┐  ┌──────────┐  ┌────────────┐  │
│  Whitelist│ │  │ Ingester │→ │ Scanner  │→ │   Judge    │  │
│  - Meta   │ │  │ (Sprint1)│  │(Sprint 2)│  │ (Sprint 3) │  │
│  - Scores │ │  └─────────┘  └──────────┘  └────────────┘  │
└───────────┘ │       ↕              ↕             ↕          │
              │  ┌──────────────────────────────────────────┐ │
              │  │            DuckDB (Local OLAP)           │ │
              │  │  trades · volumes · z-scores · signals   │ │
              │  └──────────────────────────────────────────┘ │
              └───────────────────────────────────────────────┘
                            ↕                ↕
                 ┌──────────────┐  ┌──────────────────┐
                 │ Polygon RPC  │  │   AWS Bedrock    │
                 │(funding chk) │  │ (Llama3/Claude)  │
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
| 2 | **Behavioral** | Supabase + Polygon | Wallet win-rate history, fresh-wallet funding anomalies |
| 3 | **Intelligence** | AWS Bedrock + Search API | LLM cross-references trade against live news; scores 1-100 |

## Repository Layout

```
penumbra/
├── docs/                    # All project documentation
│   ├── architecture/        # ADRs and system design
│   ├── integrations/        # Per-service integration guides
│   └── sprints/             # Sprint specs and acceptance criteria
├── sentinel/                # Python core engine (ingester, scanner, judge)
│   ├── ingester/            # Sprint 1 — WebSocket listener & DuckDB writer
│   ├── scanner/             # Sprint 2 — Statistical signal engine
│   ├── judge/               # Sprint 3 — Bedrock reasoning layer
│   ├── alerts/              # Alert service (TODO: decide delivery mechanism)
│   ├── api/                 # FastAPI gateway
│   ├── db/                  # DuckDB + Supabase schema & helpers
│   └── config.py            # Centralized settings (Pydantic BaseSettings)
├── dashboard/               # Next.js + Tremor frontend (Sprint 4)
├── scripts/                 # One-off utilities (backfill, seed, healthcheck)
├── tests/                   # Mirrors sentinel/ structure
├── docker-compose.yml       # Local dev stack
├── pyproject.toml           # Python project config (uv/pip)
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

# 4. Start the ingester
python -m sentinel.ingester

# 5. (Later) Start the dashboard
cd dashboard && npm install && npm run dev
```

## Key Decisions

See [docs/architecture/](docs/architecture/) for full ADRs. Highlights:

- **DuckDB over ClickHouse/TimescaleDB**: Zero-ops, in-process OLAP. Perfect for a solo analyst running on a laptop or $5 VPS.
- **Modified Z-Score**: We use MAD (Median Absolute Deviation) instead of standard deviation to handle fat-tailed volume distributions.
- **Bedrock budget cap**: Max 50 LLM calls/day to keep costs <$2/day. Queue prioritizes highest statistical-score trades.
- **Abstracted data source**: Polymarket client is behind an interface so we can swap to a different prediction market later.

## Alerts

> **TODO:** Decide on an alert delivery mechanism for high-suspicion signals (score ≥ 80). Options to evaluate:
> - Telegram bot (low latency, mobile push)
> - Slack webhook (team-friendly)
> - Email (AWS SES / Resend)
> - Discord webhook
> - Plain webhook (push to any endpoint)
>
> For now, the pipeline stores all signals in DuckDB + Supabase and surfaces them on the dashboard. Alert delivery is stubbed in `sentinel/alerts/`.

## Sprints

| Sprint | Name | Goal | Docs |
|--------|------|------|------|
| 0 | **Foundation** | Repo, docs, config, integrations research | [Sprint 0](docs/sprints/sprint-0-foundation.md) |
| 1 | **The Hose** | Stream trades → DuckDB | [Sprint 1](docs/sprints/sprint-1-hose.md) |
| 2 | **The Scanner** | Statistical signal detection | [Sprint 2](docs/sprints/sprint-2-scanner.md) |
| 3 | **The Judge** | LLM reasoning + news cross-ref | [Sprint 3](docs/sprints/sprint-3-judge.md) |
| 4 | **The Sentinel** | Dashboard + alerts | [Sprint 4](docs/sprints/sprint-4-sentinel.md) |

## License

Private — not for redistribution.