"""Main entry point for running Penumbra."""

import asyncio
import sys

import structlog

from sentinel.config import settings

logger = structlog.get_logger()


def main() -> None:
    """Run the Penumbra pipeline."""
    # TODO: Sprint 1+ — Wire up ingester, scanner, judge, and alert service
    logger.info(
        "Penumbra starting",
        duckdb_path=str(settings.duckdb_path),
        categories=settings.polymarket_categories,
        log_level=settings.log_level,
    )
    print("Penumbra — Sprint 0 complete. Run individual sprints as they are implemented.")
    print(f"  DuckDB path: {settings.duckdb_path}")
    print(f"  Categories:  {settings.polymarket_categories}")
    print(f"  Bedrock T1:  {settings.bedrock_tier1_model} (limit: {settings.bedrock_tier1_daily_limit}/day)")
    print(f"  Bedrock T2:  {settings.bedrock_tier2_model} (limit: {settings.bedrock_tier2_daily_limit}/day)")
    sys.exit(0)


if __name__ == "__main__":
    main()
