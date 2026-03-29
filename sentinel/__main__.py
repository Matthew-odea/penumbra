"""Main entry point for running Penumbra.

Usage::

    python -m sentinel                  # Full pipeline (Ingester + Scanner + Judge)
    python -m sentinel --with-api       # Pipeline + FastAPI server on :8000
    python -m sentinel --dry-run        # Pipeline in dry-run mode (no DB writes)
    python -m sentinel --timeout 60     # Auto-stop after 60 seconds
"""

from __future__ import annotations

import argparse
import asyncio

import structlog
import uvicorn

from sentinel.config import settings
from sentinel.ingester.__main__ import run_ingester

logger = structlog.get_logger()


async def _run_all(
    *,
    dry_run: bool = False,
    timeout: int | None = None,
    with_api: bool = False,
) -> None:
    """Run the full Penumbra system: pipeline + optional API server."""
    tasks: list[asyncio.Task] = []

    # 1. Core pipeline (Ingester → Scanner → Judge)
    tasks.append(
        asyncio.create_task(
            run_ingester(dry_run=dry_run, timeout=timeout),
            name="pipeline",
        )
    )

    # 2. Optional: FastAPI server alongside the pipeline
    if with_api and not dry_run:
        from sentinel.api.main import app

        config = uvicorn.Config(
            app,
            host=settings.api_host,
            port=settings.api_port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        tasks.append(
            asyncio.create_task(server.serve(), name="api_server")
        )
        logger.info("API server starting", host=settings.api_host, port=settings.api_port)

    logger.info(
        "Penumbra starting",
        mode="DRY RUN" if dry_run else "LIVE",
        with_api=with_api,
        duckdb_path=str(settings.duckdb_path),
    )

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Penumbra — Polymarket informed-flow detection system",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print events as JSON, skip DB writes")
    parser.add_argument("--timeout", type=int, default=None, help="Stop after N seconds")
    parser.add_argument("--with-api", action="store_true", help="Also start the FastAPI server on :8000")
    args = parser.parse_args()

    try:
        asyncio.run(
            _run_all(
                dry_run=args.dry_run,
                timeout=args.timeout,
                with_api=args.with_api,
            )
        )
    except KeyboardInterrupt:
        logger.info("Penumbra stopped by user")


if __name__ == "__main__":
    main()
