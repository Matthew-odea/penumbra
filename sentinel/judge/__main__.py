"""CLI entry point for the Judge.

Usage:
    python -m sentinel.judge --replay             # Process all un-judged signals
    python -m sentinel.judge --replay --limit 5   # Process up to 5 signals
    python -m sentinel.judge --replay --dry-run   # Preview without Bedrock calls
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

import structlog

from sentinel.config import settings
from sentinel.db.init import init_schema
from sentinel.judge.budget import BudgetManager
from sentinel.judge.pipeline import Judge
from sentinel.judge.store import Alert
from sentinel.scanner.scorer import Signal

logger = structlog.get_logger()


def _load_signals_from_db(
    conn,
    *,
    limit: int | None = None,
    unjudged_only: bool = True,
) -> list[Signal]:
    """Load signals from DuckDB for replay mode.

    Args:
        conn: DuckDB connection.
        limit: Maximum number of signals to load.
        unjudged_only: If True, only load signals without a matching
            row in ``signal_reasoning``.

    Returns:
        List of ``Signal`` objects.
    """
    sql = """
    SELECT
        s.signal_id, s.trade_id, s.market_id, s.wallet, s.side,
        s.price, s.size_usd, s.trade_timestamp,
        s.volume_z_score, s.modified_z_score, s.price_impact,
        s.wallet_win_rate, s.wallet_total_trades, s.is_whitelisted,
        s.funding_anomaly, s.funding_age_minutes,
        s.statistical_score, s.created_at
    FROM signals s
    """
    if unjudged_only:
        sql += """
    LEFT JOIN signal_reasoning sr ON s.signal_id = sr.signal_id
    WHERE sr.signal_id IS NULL
    """

    sql += " ORDER BY s.statistical_score DESC"

    if limit:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql).fetchall()
    signals = []
    for row in rows:
        sig = Signal(
            signal_id=row[0],
            trade_id=row[1],
            market_id=row[2],
            wallet=row[3],
            side=row[4],
            price=float(row[5]) if row[5] is not None else 0.0,
            size_usd=float(row[6]) if row[6] is not None else 0.0,
            trade_timestamp=row[7] or datetime.now(tz=UTC),
            volume_z_score=float(row[8]) if row[8] is not None else 0.0,
            modified_z_score=float(row[9]) if row[9] is not None else 0.0,
            price_impact=float(row[10]) if row[10] is not None else 0.0,
            wallet_win_rate=float(row[11]) if row[11] is not None else None,
            wallet_total_trades=int(row[12]) if row[12] is not None else None,
            is_whitelisted=bool(row[13]),
            funding_anomaly=bool(row[14]),
            funding_age_minutes=int(row[15]) if row[15] is not None else None,
            statistical_score=int(row[16]) if row[16] is not None else 0,
            created_at=row[17] or datetime.now(tz=UTC),
        )
        signals.append(sig)

    return signals


async def _run_replay(
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> None:
    """Replay mode: load signals from DuckDB and run the judge on each."""
    conn = init_schema()

    signals = _load_signals_from_db(conn, limit=limit, unjudged_only=True)
    if not signals:
        logger.info("No unjudged signals found in DuckDB")
        conn.close()
        return

    logger.info("Loaded signals for replay", count=len(signals))

    # Print budget status
    budget = BudgetManager(conn)
    for tier, status in budget.get_status().items():
        logger.info(
            f"Budget {tier}",
            used=status.calls_used,
            limit=status.calls_limit,
            remaining=status.remaining,
        )

    # Set up queues
    judge_queue: asyncio.Queue[Signal] = asyncio.Queue()
    alert_queue: asyncio.Queue[Alert] = asyncio.Queue()

    # Enqueue all signals
    for sig in signals:
        await judge_queue.put(sig)

    judge = Judge(
        conn,
        judge_queue=judge_queue,
        alert_queue=alert_queue,
        dry_run=dry_run,
    )

    # Run until queue is drained
    async def _drain() -> None:
        await judge_queue.join()
        judge.stop()

    drain_task = asyncio.create_task(_drain())
    judge_task = asyncio.create_task(judge.run())

    await asyncio.gather(drain_task, judge_task)

    # Report results
    logger.info(
        "Replay complete",
        processed=judge.signals_processed,
        tier1_calls=judge.tier1_calls,
        tier2_calls=judge.tier2_calls,
        alerts=judge.alerts_emitted,
        skipped_budget=judge.skipped_budget,
    )

    # Print alerts
    while not alert_queue.empty():
        alert = alert_queue.get_nowait()
        print(
            f"  ALERT: score={alert.score} "
            f"signal={alert.signal.signal_id[:8]}... "
            f"market={alert.signal.market_id[:8]}... "
            f"reasoning={alert.reasoning[:80]}"
        )

    # Print reasoning table count
    count = conn.execute("SELECT COUNT(*) FROM signal_reasoning").fetchone()[0]
    logger.info("signal_reasoning rows", total=count)

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Penumbra Judge — LLM reasoning layer")
    parser.add_argument("--replay", action="store_true", help="Process existing signals from DuckDB")
    parser.add_argument("--limit", type=int, default=None, help="Max signals to process in replay")
    parser.add_argument("--dry-run", action="store_true", help="Preview without Bedrock calls")
    args = parser.parse_args()

    if args.replay:
        try:
            asyncio.run(_run_replay(limit=args.limit, dry_run=args.dry_run))
        except KeyboardInterrupt:
            logger.info("Judge interrupted by user")
    else:
        parser.print_help()
        print("\nHint: Use --replay to process existing signals from DuckDB")


if __name__ == "__main__":
    main()
