"""Ingester entry point — run as ``python -m sentinel.ingester``.

Starts the WebSocket listener, batch writer, and periodic market sync
as concurrent async tasks.

Flags::

    --dry-run     Print trades as JSON instead of writing to DuckDB
    --timeout N   Stop after N seconds (useful for smoke tests)
    --markets     Comma-separated condition_ids to subscribe to
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import structlog

from sentinel.config import settings
from sentinel.db.init import init_schema
from sentinel.ingester.listener import Listener
from sentinel.ingester.markets import sync_markets
from sentinel.ingester.models import Trade
from sentinel.ingester.writer import BatchWriter

logger = structlog.get_logger()


async def _periodic_market_sync(conn: object, interval_hours: int) -> None:
    """Re-sync market metadata on a timer."""
    while True:
        try:
            count = await sync_markets(conn)
            logger.info("Periodic market sync complete", count=count)
        except Exception as exc:
            logger.error("Market sync failed", error=str(exc))
        await asyncio.sleep(interval_hours * 3600)


async def run_ingester(
    *,
    dry_run: bool = False,
    timeout: int | None = None,
    market_ids: list[str] | None = None,
) -> None:
    """Main async entry point for the ingester pipeline."""
    conn = None if dry_run else init_schema()

    # Scanner queue — placeholder for Sprint 2 consumption
    scanner_queue: asyncio.Queue[list[Trade]] = asyncio.Queue()

    writer = BatchWriter(
        conn,
        scanner_queue=scanner_queue,
        dry_run=dry_run,
    )

    listener = Listener(
        on_trade=writer.add,
        market_ids=market_ids or [],
        dry_run=dry_run,
    )

    tasks: list[asyncio.Task] = []

    # 1. Market metadata sync (skip in dry-run since we have no DB)
    if not dry_run and conn is not None:
        logger.info("Running initial market sync...")
        try:
            count = await sync_markets(conn)
            logger.info("Initial market sync complete", count=count)
        except Exception as exc:
            logger.warning("Initial market sync failed (continuing)", error=str(exc))

        # Periodic re-sync
        tasks.append(
            asyncio.create_task(
                _periodic_market_sync(conn, settings.market_sync_interval_hours),
                name="market_sync",
            )
        )

    # 2. Batch writer timer (flush on interval even if batch_size not reached)
    tasks.append(
        asyncio.create_task(writer.run_timer(), name="writer_timer")
    )

    # 3. WebSocket listener
    tasks.append(
        asyncio.create_task(listener.run(), name="ws_listener")
    )

    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(
        f"Ingester running [{mode}]",
        markets=len(market_ids) if market_ids else "all",
        batch_size=settings.ingester_batch_size,
        flush_interval=settings.ingester_flush_interval_seconds,
    )

    try:
        if timeout:
            await asyncio.sleep(timeout)
            logger.info("Timeout reached — shutting down", timeout_s=timeout)
        else:
            # Run until externally cancelled
            await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        listener.stop()
        # Final flush of any remaining trades
        await writer.flush()
        for t in tasks:
            t.cancel()
        logger.info(
            "Ingester stopped",
            total_trades=writer.total_written,
            ws_trades=listener.trade_count,
        )
        if conn is not None:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Penumbra trade ingester")
    parser.add_argument("--dry-run", action="store_true", help="Print trades as JSON, skip DB")
    parser.add_argument("--timeout", type=int, default=None, help="Stop after N seconds")
    parser.add_argument("--markets", type=str, default="", help="Comma-separated condition_ids")
    args = parser.parse_args()

    market_ids = [m.strip() for m in args.markets.split(",") if m.strip()] if args.markets else None

    try:
        asyncio.run(
            run_ingester(
                dry_run=args.dry_run,
                timeout=args.timeout,
                market_ids=market_ids,
            )
        )
    except KeyboardInterrupt:
        logger.info("Ingester interrupted by user")


if __name__ == "__main__":
    main()
