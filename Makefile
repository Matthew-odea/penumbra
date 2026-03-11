# =============================================================================
# Penumbra — Makefile
# =============================================================================

.PHONY: help install dev setup-env test lint format check db-init run-ingester run-scanner run-judge run-api run-dashboard clean

PYTHON := python
UV := uv

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Setup ─────────────────────────────────────────────────────────────────────

install: ## Install production dependencies
	$(PYTHON) -m pip install -e .

dev: ## Install with dev dependencies
	$(PYTHON) -m pip install -e ".[dev]"
	pre-commit install

setup-env: ## Generate .env from filled-in SETUP_KEYS.md
	$(PYTHON) scripts/setup_env.py

# ── Database ──────────────────────────────────────────────────────────────────

db-init: ## Initialize DuckDB schema
	$(PYTHON) -m sentinel.db.init

db-reset: ## Delete and reinitialize DuckDB
	rm -f data/sentinel.duckdb
	$(PYTHON) -m sentinel.db.init

# ── Running ───────────────────────────────────────────────────────────────────

run: ## Run the full pipeline
	$(PYTHON) -m sentinel

run-ingester: ## Run the trade ingester (Sprint 1)
	$(PYTHON) -m sentinel.ingester

run-scanner: ## Run the statistical scanner (Sprint 2)
	$(PYTHON) -m sentinel.scanner

run-judge: ## Run the AI judge (Sprint 3)
	$(PYTHON) -m sentinel.judge

run-api: ## Run the FastAPI gateway
	uvicorn sentinel.api.main:app --host 0.0.0.0 --port 8000 --reload

run-dashboard: ## Run the Vite+React dashboard
	cd dashboard && npm run dev

# ── Quality ───────────────────────────────────────────────────────────────────

test: ## Run all tests (excluding integration)
	pytest -m "not integration" --cov=sentinel --cov-report=term-missing

test-all: ## Run all tests including integration
	pytest --cov=sentinel --cov-report=term-missing

test-integration: ## Run only integration tests
	pytest -m integration -v

lint: ## Run linter
	ruff check sentinel/ tests/

format: ## Auto-format code
	ruff format sentinel/ tests/
	ruff check --fix sentinel/ tests/

typecheck: ## Run mypy type checker
	mypy sentinel/

check: lint typecheck test ## Run all checks (lint + typecheck + test)

# ── Utilities ─────────────────────────────────────────────────────────────────

backfill: ## Backfill 7 days of trades for configured categories
	$(PYTHON) scripts/backfill.py

clean: ## Remove build artifacts and caches
	rm -rf .pytest_cache .mypy_cache .ruff_cache __pycache__
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

docker-up: ## Start local dev stack via Docker Compose
	docker compose up -d

docker-down: ## Stop local dev stack
	docker compose down
