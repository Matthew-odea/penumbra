# Sprint 0: Foundation

**Goal:** Repo structure, documentation, integration research, configuration, and project scaffolding.

**Duration:** 1 session  
**Status:** ✅ Complete

## Deliverables

- [x] Repository structure with clear separation of concerns
- [x] README with architecture overview, quick start, and sprint roadmap
- [x] Architecture Decision Records (ADRs):
  - ADR-001: DuckDB as Local OLAP Engine
  - ADR-002: Modified Z-Score over Standard Z-Score
  - ADR-003: Bedrock Budget Cap & Call Queue
  - ADR-004: Pipeline Architecture (Single-Writer Coordination)
- [x] Integration docs for all external services:
  - Polymarket CLOB API (WebSocket + REST)
  - Supabase (Postgres + Realtime + Storage)
  - AWS Bedrock (Llama 3 + Claude 3.5)
  - Polygon RPC / Alchemy (Wallet funding analysis)
  - Tavily Search (News context for Judge)
  - Alerts (TODO: decide delivery mechanism)
- [x] Python project configuration (`pyproject.toml`)
- [x] Environment variable template (`.env.example`)
- [x] Centralized settings module (`sentinel/config.py`)
- [x] DuckDB schema definitions (`sentinel/db/`)
- [x] Supabase migration SQL
- [x] Docker Compose for local development
- [x] Makefile with common commands
- [x] Contributing guide & development workflow
- [x] Sprint 1-4 detailed specifications

## Assumptions Challenged

See the main README and ADRs for full analysis. Key challenges:

1. **Z-score normality assumption** → Modified Z-Score (ADR-002)
2. **Polygon RPC scaling** → Alchemy Transfers API instead
3. **Bedrock cost runaway** → Two-tier budget cap (ADR-003)
4. **DuckDB concurrency** → Single-process async pipeline (ADR-004)
5. **Polymarket SDK stability** → Abstraction layer + pinned versions
6. **Win-rate cold start** → Curated seed list + incremental backfill
