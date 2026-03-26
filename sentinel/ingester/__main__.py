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
import time

import structlog

from sentinel.config import settings
from sentinel.db.init import init_schema
from sentinel.ingester.listener import Listener
from sentinel.ingester.markets import (
    fetch_active_markets,
    fetch_market_by_id,
    get_priority_market_ids,
    sync_markets,
    upsert_markets,
)
from sentinel.ingester.models import BookEvent, Trade
from sentinel.ingester.poller import TradePoller
from sentinel.ingester.writer import BatchWriter
from sentinel.ingester.market_scorer import MarketAttractivenessInput, score_market_attractiveness
from sentinel.judge.budget import BudgetManager
from sentinel.judge.pipeline import Judge
from sentinel.judge.store import Alert
from sentinel.scanner.pipeline import Scanner
from sentinel.scanner.scorer import Signal

logger = structlog.get_logger()

# ── Market attractiveness scoring ─────────────────────────────────────────────

_SCORING_CONCURRENCY = 3  # Parallel Nova Lite workers (reduced from 8: was OOM-killing t3.micro)


async def _market_attractiveness_scorer(
    conn: object,
    queue: asyncio.Queue[str],
) -> None:
    """Background task: score attractiveness for unscored markets via LLM.

    Spawns _SCORING_CONCURRENCY worker coroutines; each pulls market_ids from
    the queue, checks the Bedrock budget, calls Nova Lite, and writes the result
    back.  Workers are tracked via asyncio.gather so cancelling this task
    cleanly cancels all workers and lets their finally blocks call task_done().
    """
    budget = BudgetManager(conn)  # type: ignore[arg-type]

    async def _worker(worker_id: int) -> None:
        while True:
            market_id = await queue.get()
            try:
                row = conn.execute(  # type: ignore[attr-defined]
                    """SELECT question, category, end_date, liquidity_usd,
                              attractiveness_score
                       FROM markets WHERE market_id = ?""",
                    [market_id],
                ).fetchone()

                if row is None:
                    continue

                # Skip if already scored (race between enqueue and processing)
                if row[4] is not None:
                    continue

                # Gate on Bedrock budget — market scoring shares the tier1 pool
                if not budget.try_record_call("tier1"):
                    # Budget exhausted for today.  Drain the rest of the queue
                    # without any DuckDB reads (avoids blocking the event loop
                    # for 30k synchronous SELECT calls), then sleep until the
                    # budget resets at midnight UTC.
                    remaining = queue.qsize()
                    logger.warning(
                        "Market scoring budget exhausted — draining queue and sleeping",
                        market_id=market_id,
                        worker_id=worker_id,
                        queue_remaining=remaining,
                    )
                    # Drain quickly without DB work
                    while True:
                        try:
                            queue.get_nowait()
                            queue.task_done()
                        except asyncio.QueueEmpty:
                            break
                    # Sleep until midnight UTC (budget reset)
                    from datetime import timedelta
                    _now = datetime.now(UTC)
                    _reset = (_now.replace(
                        hour=0, minute=0, second=0, microsecond=0,
                    ) + timedelta(days=1))
                    _sleep = max((_reset - _now).total_seconds(), 60)
                    logger.info(
                        "Market scoring paused until budget reset",
                        resume_utc=_reset.isoformat(),
                        sleep_hours=round(_sleep / 3600, 1),
                    )
                    await asyncio.sleep(_sleep)
                    continue

                question, category, end_date, liquidity_usd, _ = row
                end_date_str = end_date.isoformat() if end_date else "unknown"
                liquidity = float(liquidity_usd or 0)

                inp = MarketAttractivenessInput(
                    question=question or "",
                    tags=category or "",
                    end_date_str=end_date_str,
                    liquidity_usd=liquidity,
                )
                result = await score_market_attractiveness(inp)

                conn.execute(  # type: ignore[attr-defined]
                    """UPDATE markets
                       SET attractiveness_score = ?, attractiveness_reason = ?
                       WHERE market_id = ?""",
                    [result.score, result.reason, market_id],
                )
            except Exception as exc:
                logger.warning(
                    "Market scoring failed",
                    market_id=market_id,
                    error=str(exc),
                )
            finally:
                queue.task_done()

    workers = [
        asyncio.create_task(_worker(i), name=f"market-scorer-{i}")
        for i in range(_SCORING_CONCURRENCY)
    ]
    try:
        # return_exceptions=True: one worker crash doesn't kill the rest
        await asyncio.gather(*workers, return_exceptions=True)
    finally:
        # Ensure all workers are cancelled and awaited so their finally blocks
        # (queue.task_done) run before this coroutine returns.
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


def _enqueue_unscored_markets(conn: object, queue: asyncio.Queue[str]) -> int:
    """Query DB for markets without attractiveness scores and enqueue them."""
    try:
        rows = conn.execute(  # type: ignore[attr-defined]
            "SELECT market_id FROM markets WHERE attractiveness_score IS NULL"
        ).fetchall()
        count = 0
        for (market_id,) in rows:
            queue.put_nowait(market_id)
            count += 1
        if count:
            logger.info("Queued markets for attractiveness scoring", count=count)
        return count
    except Exception as exc:
        logger.warning("Failed to enqueue unscored markets", error=str(exc))
        return 0


# ── Periodic tasks ────────────────────────────────────────────────────────────

async def _on_demand_market_resolver(
    conn: object,
    unknown_queue: asyncio.Queue[str],
    scoring_queue: asyncio.Queue[str],
) -> None:
    """Background task: fetch metadata for market_ids not yet in the DB."""
    pending: set[str] = set()
    while True:
        market_id = await unknown_queue.get()
        if market_id in pending:
            unknown_queue.task_done()
            continue
        pending.add(market_id)
        try:
            raw = await fetch_market_by_id(market_id)
            if raw:
                upsert_markets(conn, [raw])  # type: ignore[arg-type]
                # Queue newly discovered market for LLM scoring
                scoring_queue.put_nowait(market_id)
                logger.info("On-demand market metadata fetched", market_id=market_id)
            else:
                logger.debug("Market not found via REST", market_id=market_id)
        except Exception as exc:
            logger.warning("On-demand market fetch failed", market_id=market_id, error=str(exc))
        finally:
            unknown_queue.task_done()
        await asyncio.sleep(1.0)  # gentle rate limit


async def _periodic_market_sync(
    conn: object,
    interval_hours: int,
    scoring_queue: asyncio.Queue[str],
) -> None:
    """Re-sync all market metadata on a timer and queue any newly unscored markets."""
    while True:
        await asyncio.sleep(interval_hours * 3600)
        try:
            count = await sync_markets(conn)  # type: ignore[arg-type]
            logger.info("Periodic market sync complete", count=count)
            _enqueue_unscored_markets(conn, scoring_queue)
        except Exception as exc:
            logger.error("Market sync failed", error=str(exc))


async def _periodic_hot_market_refresh(
    conn: object,
    poller: TradePoller,
    interval_seconds: int | None = None,
) -> None:
    """Refresh the hot-tier market list from the DB priority formula.

    Replaces the old /sampling-markets API call with a local DB query
    using the attractiveness × time_weight × uncertainty × liquidity formula.
    """
    interval = interval_seconds or settings.hot_market_refresh_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            new_ids = get_priority_market_ids(conn)  # type: ignore[arg-type]
            poller.update_markets(new_ids)
            logger.info("Hot market list refreshed from priority formula", count=len(new_ids))
        except Exception as exc:
            logger.warning("Hot market refresh failed", error=str(exc))


async def _periodic_status(
    writer: BatchWriter,
    listener: Listener,
    poller: TradePoller,
    scanner: Scanner | None,
    judge: Judge | None,
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


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_ingester(
    *,
    dry_run: bool = False,
    timeout: int | None = None,
    asset_ids: list[str] | None = None,
) -> None:
    """Main async entry point for the ingester pipeline."""
    conn = None if dry_run else init_schema()

    scanner_queue: asyncio.Queue[list[Trade | BookEvent]] = asyncio.Queue()
    judge_queue: asyncio.Queue[Signal] = asyncio.Queue()
    alert_queue: asyncio.Queue[Alert] = asyncio.Queue()
    scoring_queue: asyncio.Queue[str] = asyncio.Queue()

    writer = BatchWriter(
        conn,
        scanner_queue=scanner_queue,
        dry_run=dry_run,
    )

    # Track market_ids already in the DB to detect unknown ones on-the-fly
    _known_market_ids: set[str] = set()
    _unknown_market_queue: asyncio.Queue[str] = asyncio.Queue()

    if not dry_run and conn is not None:
        try:
            rows = conn.execute("SELECT market_id FROM markets").fetchall()
            _known_market_ids = {r[0] for r in rows}
        except Exception as exc:
            logger.warning(
                "Failed to preload market IDs from DB — on-demand resolver will handle them",
                error=str(exc),
            )

    _market_ids_lock = asyncio.Lock()

    async def _on_trade(trade: Trade) -> None:
        if not dry_run:
            async with _market_ids_lock:
                if trade.market_id not in _known_market_ids:
                    _known_market_ids.add(trade.market_id)
                    await _unknown_market_queue.put(trade.market_id)
        await writer.add(trade)

    book_event_count = 0
    _last_snapshot: dict[str, float] = {}
    _SNAPSHOT_INTERVAL = 30.0

    async def _on_book_event(evt: BookEvent) -> None:
        nonlocal book_event_count
        book_event_count += 1
        if dry_run:
            print(json.dumps(evt.as_dict()))
        if scanner_queue is not None:
            await scanner_queue.put([evt])

        if not dry_run and conn is not None:
            now = time.monotonic()
            if now - _last_snapshot.get(evt.market_id, 0) >= _SNAPSHOT_INTERVAL:
                _last_snapshot[evt.market_id] = now
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO book_snapshots"
                        " (market_id, asset_id, ts, best_bid, best_ask)"
                        " VALUES (?, ?, ?, ?, ?)",
                        [
                            evt.market_id,
                            evt.asset_id,
                            evt.timestamp,
                            float(evt.best_bid),
                            float(evt.best_ask),
                        ],
                    )
                except Exception as exc:
                    logger.debug("Book snapshot write failed", market=evt.market_id, error=str(exc))

    # ── Resolve initial hot-tier markets ─────────────────────────────────
    condition_ids: list[str] = []
    if not asset_ids:
        logger.info("No asset IDs provided — bootstrapping from active markets...")
        try:
            # Use sampling-markets for WS subscription asset IDs (token IDs)
            active_markets = await fetch_active_markets(
                limit=settings.hot_market_count,
            )
            condition_ids = [cid for cid, _ in active_markets]
            asset_ids = [aid for _, aids in active_markets for aid in aids]
            logger.info(
                "WS bootstrap from sampling-markets",
                markets=len(condition_ids),
                assets=len(asset_ids),
            )
        except Exception as exc:
            logger.error("Failed to bootstrap active markets", error=str(exc))
            asset_ids = []

    listener = Listener(
        on_trade=_on_trade,
        on_book_event=_on_book_event,
        asset_ids=asset_ids,
        dry_run=dry_run,
    )

    poller = TradePoller(
        on_trade=_on_trade,
        condition_ids=condition_ids,
        dry_run=dry_run,
    )

    tasks: list[asyncio.Task] = []

    if not dry_run and conn is not None:
        # 1. Initial full market sync (all markets, no category filter)
        logger.info("Running initial market sync (all markets)...")
        try:
            count = await sync_markets(conn)
            logger.info("Initial market sync complete", count=count)
        except Exception as exc:
            logger.warning("Initial market sync failed (continuing)", error=str(exc))

        # 2. Queue all unscored markets for LLM attractiveness scoring
        _enqueue_unscored_markets(conn, scoring_queue)

        # 3. Now that we have the full DB, build the initial hot tier from priority formula
        try:
            priority_ids = get_priority_market_ids(conn)
            if priority_ids:
                poller.update_markets(priority_ids)
                logger.info("Initial hot tier from priority formula", count=len(priority_ids))
        except Exception as exc:
            logger.warning("Failed to build initial hot tier from DB", error=str(exc))

        # 4. On-demand resolver for unknown market_ids seen in trades
        tasks.append(
            asyncio.create_task(
                _on_demand_market_resolver(conn, _unknown_market_queue, scoring_queue),
                name="market_resolver",
            )
        )

        # 5. Market attractiveness scoring queue (8 parallel workers)
        tasks.append(
            asyncio.create_task(
                _market_attractiveness_scorer(conn, scoring_queue),
                name="market_scorer",
            )
        )

        # 6. Periodic market sync (every 2h)
        tasks.append(
            asyncio.create_task(
                _periodic_market_sync(conn, settings.market_sync_interval_hours, scoring_queue),
                name="market_sync",
            )
        )

        # 7. Periodic hot-market refresh from DB priority formula (every 30 min)
        tasks.append(
            asyncio.create_task(
                _periodic_hot_market_refresh(conn, poller),
                name="hot_market_refresh",
            )
        )

    # 8. Batch writer timer
    tasks.append(
        asyncio.create_task(writer.run_timer(), name="writer_timer")
    )

    # 9. WebSocket listener
    tasks.append(
        asyncio.create_task(listener.run(), name="ws_listener")
    )

    # 10. REST trade poller (hot tier only)
    if poller._condition_ids:
        tasks.append(
            asyncio.create_task(poller.run(), name="trade_poller")
        )

    # 11. Scanner
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

    # 12. Judge
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

    # 13. Periodic aggregate status
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
            await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        listener.stop()
        poller.stop()

        await writer.flush()

        drain_timeout = 30.0
        drain_awaitables = [asyncio.wait_for(scanner_queue.join(), timeout=drain_timeout)]
        if judge is not None:
            drain_awaitables.append(asyncio.wait_for(judge_queue.join(), timeout=drain_timeout))
        drain_results = await asyncio.gather(*drain_awaitables, return_exceptions=True)
        for result in drain_results:
            if isinstance(result, asyncio.TimeoutError):
                logger.warning("Queue drain timed out — some in-flight signals may be lost")

        if scanner is not None:
            scanner.stop()
        if judge is not None:
            judge.stop()

        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info(
            "Ingester stopped",
            total_trades=writer.total_written,
            ws_book_events=listener.book_event_count,
            polled_trades=poller.trade_count,
            poll_cycles=poller.poll_count,
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
    parser.add_argument("--assets", type=str, default="", help="Comma-separated token_ids")
    parser.add_argument("--markets", type=str, default="", help="Comma-separated condition_ids (deprecated)")
    args = parser.parse_args()

    asset_ids = [a.strip() for a in args.assets.split(",") if a.strip()] if args.assets else None

    if args.markets and not asset_ids:
        logger.warning("--markets flag requires asset_id resolution. Use --assets with token IDs instead.")

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
