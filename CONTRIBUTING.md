# Contributing to Penumbra

## Development Setup

### Prerequisites

- Python 3.11+
- Node.js 20+ (for dashboard)
- Git

### Quick Setup

```bash
# Clone
git clone <repo-url> && cd penumbra

# Python environment
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"

# Pre-commit hooks
pre-commit install

# Environment variables
cp .env.example .env
# Edit .env with your API keys

# Initialize database
python -m sentinel.db.init

# Verify setup
make check
```

### Dashboard Setup (Sprint 4)

```bash
cd dashboard
npm install
cp .env.example .env.local
# Edit .env.local with Supabase keys
npm run dev
```

## Project Structure

```
sentinel/                 # Python core — the brain
├── config.py             # All settings (Pydantic BaseSettings)
├── __main__.py           # Entry point
├── db/                   # DuckDB + Supabase schema
├── ingester/             # Sprint 1 — WebSocket → DuckDB
├── scanner/              # Sprint 2 — Z-scores, price impact, wallet profiling
├── judge/                # Sprint 3 — Bedrock LLM reasoning
├── alerts/               # Alert service (TBD)
└── api/                  # FastAPI gateway

dashboard/                # Next.js frontend — the face
tests/                    # Mirrors sentinel/ structure
scripts/                  # One-off utilities
docs/                     # Architecture + integration docs
```

## Workflow

### Sprint-Based Development

Each sprint is self-contained. Work on one sprint at a time:

1. Read the sprint spec in `docs/sprints/sprint-N-*.md`
2. Create a feature branch: `git checkout -b sprint-N/feature-name`
3. Implement, test, commit
4. Verify Definition of Done from the sprint spec
5. Merge to `main`

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(ingester): add WebSocket listener for Polymarket trades
fix(scanner): handle zero-MAD markets in Z-score calculation
docs(adr): add ADR-005 for caching strategy
test(judge): add unit tests for prompt construction
chore: update dependencies
```

### Code Style

- **Formatter**: Ruff (auto-format on save)
- **Linter**: Ruff (strict mode)
- **Type checker**: Mypy
- **Line length**: 100 characters
- **Python version**: 3.11+

All enforced via `make check`.

### Testing

```bash
make test              # Unit tests only
make test-integration  # Integration tests (require API keys)
make test-all          # Everything
```

**Test file naming**: `tests/<module>/test_<feature>.py`

**Markers**:
- `@pytest.mark.integration` — requires live API connections
- `@pytest.mark.slow` — takes >10 seconds

### Adding a New Integration

1. Create a doc in `docs/integrations/<service>.md`
2. Add env vars to `.env.example`
3. Add settings to `sentinel/config.py`
4. Add the dependency to `pyproject.toml`
5. Write integration code in the appropriate module
6. Add unit + integration tests

## Environment Variables

All config is loaded from `.env` via Pydantic BaseSettings. Never hardcode secrets.

To add a new setting:
1. Add to `.env.example` with a comment
2. Add to `Settings` class in `sentinel/config.py`
3. Use as `settings.your_new_setting` anywhere in the code

## Key Design Principles

1. **Single-process pipeline**: Ingester → Scanner → Judge share one DuckDB connection via asyncio queues. No multi-process complexity.
2. **Budget-aware**: Every external API call is gated by configurable limits.
3. **Fail gracefully**: If Bedrock is down, the Scanner still works. If Alchemy is down, funding checks are skipped (not fatal).
4. **Observable**: Structured logging (structlog) everywhere. Dashboard shows system health.
5. **Testable**: Each module is testable in isolation with mocked dependencies.
