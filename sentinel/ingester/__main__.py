"""Ingester entry point — run as ``python -m sentinel.ingester``.

Starts the WebSocket listener, batch writer, and periodic market sync
as concurrent async tasks.

Flags::

    --dry-run     Print events as JSON instead of writing to DuckDB
    --timeout N   Stop after N seconds (useful for smoke tests)
    --assets      Comma-separated token_ids (asset IDs) to subscribe to
    --markets     Comma-separated condition_ids — resolved to asset IDs
                  via the markets table (requires prior market sync)
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
from sentinel.ingester.markets import sync_markets, fetch_active_asset_ids, fetch_active_markets, get_all_condition_ids
from sentinel.ingester.models import BookEvent, Trade
from sentinel.ingester.poller import TradePoller
from sentinel.ingester.writer import BatchWriter
from sentinel.judge.pipeline import Judge
from sentinel.judge.store import Alert
from sentinel.scanner.pipeline import Scanner
from sentinel.scanner.scorer import Signal

logger = structlog.get_logger()


async def _periodic_market_sync(conn: object, interval_hours: int) -> None:
    """Re-sync market metadata on a timer (sleeps first to avoid duplicating initial sync)."""
    while True:
        await asyncio.sleep(interval_hours * 3600)
        try:
            count = await sync_markets(conn)
            logger.info("Periodic market sync complete", count=count)
        except Exception as exc:
            logger.error("Market sync failed", error=str(exc))


async def _periodic_status(
    writer: "BatchWriter",
    listener: "Listener",
    poller: "TradePoller",
    scanner: "Scanner | None",
    judge: "Judge | None",
    interval: int = 30,
) -> None:
    """Print a single aggregate status line every *interval* seconds."""
    prev_trades = 0
    prev_book = 0
    prev_polled = 0
    while True:
        await asyncio.sleep(interval)
        ws_t = listener.trade_count
        ws_b = listener.book_event_count
        rest_t = poller.trade_count
        d_ws = ws_t - prev_trades
        d_book = ws_b - prev_book
        d_rest = rest_t - prev_polled
        prev_trades, prev_book, prev_polled = ws_t, ws_b, rest_t

        parts: dict[str, object] = {
            "ws_trades": ws_t,
            "book_events": ws_b,
            "rest_trades": rest_t,
            "rest_cold": poller.cold_trade_count,
            "db_written": writer.total_written,
        }
        if d_ws or d_book or d_rest:
            parts["Δws"] = d_ws
            parts["Δbook"] = d_book
            parts["Δrest"] = d_rest
        if scanner is not None:
            parts["scanned"] = scanner.trades_scanned
            parts["signals"] = scanner.signals_emitted
        if judge is not None:
            parts["judged"] = judge.signals_processed
            parts["alerts"] = judge.alerts_emitted
        logger.info("status", **parts)


async def run_ingester(
    *,
    dry_run: bool = False,
    timeout: int | None = None,
    asset_ids: list[str] | None = None,
) -> None:
    """Main async entry point for the ingester pipeline."""
    conn = None if dry_run else init_schema()

    # Scanner queue — consumed by the Scanner (Sprint 2)
    scanner_queue: asyncio.Queue[list[Trade | BookEvent]] = asyncio.Queue()

    # Judge queue — consumed by the Judge (Sprint 3)
    judge_queue: asyncio.Queue[Signal] = asyncio.Queue()
    alert_queue: asyncio.Queue[Alert] = asyncio.Queue()

    writer = BatchWriter(
        conn,
        scanner_queue=scanner_queue,
        dry_run=dry_run,
    )

    # Book event handler — forward to scanner queue (no DB persistence)
    book_event_count = 0

    async def _on_book_event(evt: BookEvent) -> None:
        nonlocal book_event_count
        book_event_count += 1
        if dry_run:
            print(json.dumps(evt.as_dict()))
        if scanner_queue is not None:
            await scanner_queue.put([evt])

    # ── Resolve asset IDs + condition IDs ────────────────────────────────
    condition_ids: list[str] = []
    if not asset_ids:
        logger.info("No asset IDs provided — fetching active markets...")
        try:
            active_markets = await fetch_active_markets(
                limit=settings.trade_poll_max_markets,
            )
            condition_ids = [cid for cid, _ in active_markets]
            asset_ids = [aid for _, aids in active_markets for aid in aids]
            logger.info(
                "Auto-discovered active markets",
                markets=len(condition_ids),
                assets=len(asset_ids),
            )
        except Exception as exc:
            logger.error("Failed to fetch active markets", error=str(exc))
            asset_ids = []

    listener = Listener(
        on_trade=writer.add,
        on_book_event=_on_book_event,
        asset_ids=asset_ids,
        dry_run=dry_run,
    )

    poller = TradePoller(
        on_trade=writer.add,
        condition_ids=condition_ids,
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

        # Load all DB markets for cold-tier rotation
        try:
            all_ids = get_all_condition_ids(conn)
            poller.update_cold_markets(all_ids)
        except Exception as exc:
            logger.warning("Failed to load cold-tier markets", error=str(exc))

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

    # 4. REST trade poller — hot + cold tiers
    if condition_ids or poller._cold_ids:
        tasks.append(
            asyncio.create_task(poller.run(), name="trade_poller")
        )

    # 5. Scanner — consumes from scanner_queue, emits to judge_queue
    scanner = None
    if not dry_run and conn is not None:
        scanner = Scanner(
            conn,
            scanner_queue=scanner_queue,
            judge_queue=judge_queue,
            dry_run=dry_run,
        )
        tasks.append(
            asyncio.create_task(scanner.run(), name="scanner")
        )

    # 6. Judge — consumes from judge_queue, emits to alert_queue
    judge = None
    if not dry_run and conn is not None:
        judge = Judge(
            conn,
            judge_queue=judge_queue,
            alert_queue=alert_queue,
            dry_run=dry_run,
        )
        tasks.append(
            asyncio.create_task(judge.run(), name="judge")
        )

    # 7. Periodic aggregate status line (every 30 s)
    tasks.append(
        asyncio.create_task(
            _periodic_status(writer, listener, poller, scanner, judge),
            name="status",
        )
    )

    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(
        f"Ingester running [{mode}]",
        assets=len(asset_ids) if asset_ids else 0,
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
        poller.stop()
        if scanner is not None:
            scanner.stop()
        if judge is not None:
            judge.stop()
        # Final flush of any remaining trades
        await writer.flush()
        for t in tasks:
            t.cancel()
        logger.info(
            "Ingester stopped",
            total_trades=writer.total_written,
            ws_book_events=listener.book_event_count,
            polled_trades=poller.trade_count,
            poll_cycles=poller.poll_count,
            cold_trades=poller.cold_trade_count,
            cold_cycles=poller.cold_poll_count,
            scanner_trades=scanner.trades_scanned if scanner else 0,
            scanner_signals=scanner.signals_emitted if scanner else 0,
            judge_processed=judge.signals_processed if judge else 0,
            judge_alerts=judge.alerts_emitted if judge else 0,
        )
        if conn is not None:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Penumbra trade ingester")
    parser.add_argument("--dry-run", action="store_true", help="Print events as JSON, skip DB")
    parser.add_argument("--timeout", type=int, default=None, help="Stop after N seconds")
    parser.add_argument("--assets", type=str, default="", help="Comma-separated token_ids (asset IDs)")
    parser.add_argument("--markets", type=str, default="", help="Comma-separated condition_ids (deprecated, use --assets)")
    args = parser.parse_args()

    asset_ids = [a.strip() for a in args.assets.split(",") if a.strip()] if args.assets else None

    if args.markets and not asset_ids:
        logger.warning("--markets flag requires asset_id resolution (not yet implemented). Use --assets with token IDs instead.")

    try:
        asyncio.run(
            run_ingester(
                dry_run=args.dry_run,
                timeout=args.timeout,
                asset_ids=asset_ids,
            )
        )
    except KeyboardInterrupt:
        logger.info("Ingester interrupted by user")


if __name__ == "__main__":
    main()
