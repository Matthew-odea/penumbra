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
from datetime import UTC, datetime, timedelta

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
from sentinel.budget import BudgetManager
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

                # Gate on Bedrock budget — market scoring uses its own pool
                if not budget.try_record_call("market_scoring"):
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
    """Query DB for markets without attractiveness scores and enqueue them.

    Silently drops items when the queue is full (bounded to prevent unbounded
    growth after budget exhaustion). Dropped markets will be picked up on the
    next periodic sync.
    """
    try:
        rows = conn.execute(  # type: ignore[attr-defined]
            "SELECT market_id FROM markets WHERE attractiveness_score IS NULL"
        ).fetchall()
        count = 0
        dropped = 0
        for (market_id,) in rows:
            try:
                queue.put_nowait(market_id)
                count += 1
            except asyncio.QueueFull:
                dropped += 1
        if count:
            logger.info("Queued markets for attractiveness scoring", count=count, dropped=dropped or None)
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
                _, _ = upsert_markets(conn, [raw])  # type: ignore[arg-type]
                # Queue newly discovered market for LLM scoring (drop if full)
                try:
                    scoring_queue.put_nowait(market_id)
                except asyncio.QueueFull:
                    pass  # will be picked up on next periodic sync
                logger.info("On-demand market metadata fetched", market_id=market_id)
            else:
                logger.debug("Market not found via REST", market_id=market_id)
        except Exception as exc:
            logger.warning("On-demand market fetch failed", market_id=market_id, error=str(exc))
        finally:
            unknown_queue.task_done()
        await asyncio.sleep(1.0)  # gentle rate limit


async def _sync_with_retry(
    conn: object,
    scoring_queue: asyncio.Queue[str],
    *,
    max_retries: int = 4,
    initial_backoff: float = 30.0,
) -> int:
    """Run sync_markets with exponential backoff on failure.

    Returns the number of markets synced, or 0 if all retries exhausted.
    """
    backoff = initial_backoff
    for attempt in range(1, max_retries + 1):
        try:
            count = await sync_markets(conn)  # type: ignore[arg-type]
            _enqueue_unscored_markets(conn, scoring_queue)
            return count
        except Exception as exc:
            logger.error(
                "Market sync failed",
                error=str(exc),
                attempt=attempt,
                max_retries=max_retries,
                retry_in_s=backoff if attempt < max_retries else None,
            )
            if attempt < max_retries:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 600)  # cap at 10 min
    return 0


async def _periodic_market_sync(
    conn: object,
    interval_hours: int,
    scoring_queue: asyncio.Queue[str],
) -> None:
    """Re-sync all market metadata on a timer with retry on failure."""
    while True:
        await asyncio.sleep(interval_hours * 3600)
        count = await _sync_with_retry(conn, scoring_queue)
        if count:
            logger.info("Periodic market sync complete", count=count)
        else:
            logger.error("Periodic market sync failed after all retries")


async def _periodic_hot_market_refresh(
    conn: object,
    poller: TradePoller,
    listener: Listener,
    interval_seconds: int | None = None,
) -> None:
    """Refresh the hot-tier market list from the DB priority formula.

    Replaces the old /sampling-markets API call with a local DB query
    using the attractiveness × time_weight × uncertainty × liquidity formula.
    Also updates the WS subscription for any newly-prioritised markets.
    """
    interval = interval_seconds or settings.hot_market_refresh_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            new_ids = get_priority_market_ids(conn)  # type: ignore[arg-type]
            poller.update_markets(new_ids)
            logger.info("Hot market list refreshed from priority formula", count=len(new_ids))

            # Update WS subscription for new hot-tier markets
            if new_ids:
                placeholders = ",".join("?" * len(new_ids))
                rows = conn.execute(  # type: ignore[attr-defined]
                    f"SELECT token_ids FROM markets WHERE market_id IN ({placeholders}) AND token_ids IS NOT NULL",
                    new_ids,
                ).fetchall()
                new_asset_ids = [
                    tid
                    for row in rows
                    if row[0]
                    for tid in row[0].split(",")
                    if tid
                ]
                if new_asset_ids:
                    await listener.update_subscriptions(new_asset_ids)
        except Exception as exc:
            logger.warning("Hot market refresh failed", error=str(exc))


async def _periodic_status(
    writer: BatchWriter,
    listener: Listener,
    poller: TradePoller,
    scanner: Scanner | None,
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
        if listener.reconnect_count:
            parts["ws_reconnects"] = listener.reconnect_count
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
    # Bounded queue prevents unbounded growth after budget exhaustion.
    # 5000 is enough for one full market sync (~3900 markets).
    scoring_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)

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

    _last_snapshot: dict[str, float] = {}
    _SNAPSHOT_INTERVAL = 30.0

    async def _on_book_event(evt: BookEvent) -> None:
        if dry_run:
            print(json.dumps(evt.as_dict()))

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

    poller = TradePoller(
        on_trade=_on_trade,
        condition_ids=condition_ids,
        dry_run=dry_run,
    )

    async def _on_ws_reconnect() -> None:
        """Trigger an immediate REST poll to backfill trades missed during WS gap."""
        if poller._condition_ids:
            logger.info("WS reconnected — triggering backfill poll", markets=len(poller._condition_ids))
            try:
                await poller._poll_batch(poller._condition_ids)
            except Exception as exc:
                logger.warning("Backfill poll after WS reconnect failed", error=str(exc))

    listener = Listener(
        on_trade=_on_trade,
        on_book_event=_on_book_event,
        on_reconnect=_on_ws_reconnect,
        asset_ids=asset_ids,
        dry_run=dry_run,
    )

    tasks: list[asyncio.Task] = []

    if not dry_run and conn is not None:
        # 1. Initial full market sync with retry (critical for hot tier)
        logger.info("Running initial market sync (all markets)...")
        count = await _sync_with_retry(conn, scoring_queue)
        if count:
            logger.info("Initial market sync complete", count=count)
        else:
            logger.warning("Initial market sync failed after retries — continuing with stale data")

        # 3. Now that we have the full DB, build the initial hot tier from priority formula
        #    Update BOTH the REST poller and WS listener to use the same market set.
        try:
            priority_ids = get_priority_market_ids(conn)
            if priority_ids:
                poller.update_markets(priority_ids)
                # Subscribe WS to the priority markets' token IDs
                placeholders = ",".join("?" * len(priority_ids))
                rows = conn.execute(
                    f"SELECT token_ids FROM markets WHERE market_id IN ({placeholders}) AND token_ids IS NOT NULL",
                    priority_ids,
                ).fetchall()
                priority_asset_ids = [
                    tid for row in rows if row[0]
                    for tid in row[0].split(",") if tid
                ]
                if priority_asset_ids:
                    await listener.update_subscriptions(priority_asset_ids)
                logger.info(
                    "Initial hot tier from priority formula",
                    markets=len(priority_ids),
                    ws_assets=len(priority_asset_ids) if priority_asset_ids else 0,
                )
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
                _periodic_hot_market_refresh(conn, poller, listener),
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
            dry_run=dry_run,
        )
        tasks.append(
            asyncio.create_task(scanner.run(), name="scanner")
        )

    # 12. Periodic aggregate status
    tasks.append(
        asyncio.create_task(
            _periodic_status(writer, listener, poller, scanner),
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

        try:
            await asyncio.wait_for(scanner_queue.join(), timeout=30.0)
        except TimeoutError:
            logger.warning("Scanner queue drain timed out")

        if scanner is not None:
            scanner.stop()

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
