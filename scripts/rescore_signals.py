"""Rescore all existing signals with the current scoring formula.

Reads component values (z_score, price_impact, etc.) from each signal row,
re-runs compute_statistical_score() with the v2 formula, and updates
statistical_score + scoring_version in place.

Usage:
    python scripts/rescore_signals.py [--db-path data/sentinel.duckdb] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import structlog

from sentinel.config import settings
from sentinel.db.init import init_schema
from sentinel.scanner.scorer import SCORING_VERSION, compute_statistical_score

logger = structlog.get_logger()

_FETCH_SQL = """
SELECT
    signal_id,
    modified_z_score,
    price_impact,
    wallet,
    is_whitelisted,
    funding_anomaly,
    funding_age_minutes,
    side,
    ofi_score,
    hours_to_resolution,
    market_concentration,
    wallet_total_trades,
    size_usd,
    coordination_wallet_count,
    liquidity_cliff,
    position_trade_count,
    statistical_score
FROM signals
"""

_UPDATE_SQL = """
UPDATE signals
SET statistical_score = ?, scoring_version = ?
WHERE signal_id = ?
"""


def rescore(db_path: Path | None = None, *, dry_run: bool = False, conn: object | None = None) -> dict:
    """Rescore all signals and return a summary dict.

    Args:
        db_path: Path to DuckDB file (ignored if conn is provided).
        dry_run: Show changes without writing.
        conn: Optional existing DuckDB connection (for in-process use via API).
    """
    if conn is None:
        conn = init_schema(db_path)
    rows = conn.execute(_FETCH_SQL).fetchall()  # type: ignore[union-attr]
    logger.info("Fetched signals for rescoring", count=len(rows))

    updates: list[tuple[int, int, str]] = []
    old_scores: list[int] = []
    new_scores: list[int] = []

    for row in rows:
        (
            signal_id, z_score, price_impact, wallet, is_whitelisted,
            funding_anomaly, funding_age_minutes, side, ofi_score,
            hours_to_resolution, market_concentration, wallet_total_trades,
            size_usd, coordination_wallet_count, liquidity_cliff,
            position_trade_count, old_score,
        ) = row

        new_score = compute_statistical_score(
            z_score=float(z_score or 0),
            price_impact=float(price_impact or 0),
            win_rate=None,  # Don't re-query wallet profiles — use stored values
            is_whitelisted=bool(is_whitelisted),
            funding_anomaly=bool(funding_anomaly),
            funding_age_minutes=int(funding_age_minutes) if funding_age_minutes is not None else None,
            side=str(side or "BUY"),
            ofi_score=float(ofi_score) if ofi_score is not None else None,
            hours_to_resolution=int(hours_to_resolution) if hours_to_resolution is not None else None,
            market_concentration=float(market_concentration or 0),
            wallet_total_trades=int(wallet_total_trades) if wallet_total_trades is not None else None,
            size_usd=float(size_usd or 0),
            liquidity_cliff=bool(liquidity_cliff),
            coordination_wallet_count=int(coordination_wallet_count or 0),
            position_trade_count=int(position_trade_count or 0),
        )

        old_scores.append(int(old_score or 0))
        new_scores.append(new_score)
        updates.append((new_score, SCORING_VERSION, signal_id))

    # Score change summary
    changes = Counter()
    for old, new in zip(old_scores, new_scores):
        if new > old:
            changes["increased"] += 1
        elif new < old:
            changes["decreased"] += 1
        else:
            changes["unchanged"] += 1

    summary = {
        "total": len(rows),
        "increased": changes["increased"],
        "decreased": changes["decreased"],
        "unchanged": changes["unchanged"],
        "scoring_version": SCORING_VERSION,
    }

    if old_scores:
        summary["old_mean"] = round(sum(old_scores) / len(old_scores), 1)
        summary["new_mean"] = round(sum(new_scores) / len(new_scores), 1)

    logger.info("Rescore summary", **summary)

    if dry_run:
        logger.info("DRY RUN — no changes written")
    else:
        conn.executemany(_UPDATE_SQL, updates)  # type: ignore[union-attr]
        logger.info("Scores updated in DB", count=len(updates))

    # Only close the connection if we opened it (not passed in via API)
    if db_path is not None or conn is None:
        pass  # conn was opened by init_schema — caller manages lifetime
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Rescore all signals with the current formula")
    parser.add_argument("--db-path", type=Path, default=None, help="Path to DuckDB file")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    args = parser.parse_args()

    summary = rescore(args.db_path, dry_run=args.dry_run)
    if summary["total"] == 0:
        print("No signals to rescore.")
        sys.exit(0)

    print(f"\nRescored {summary['total']} signals (v{summary['scoring_version']}):")
    print(f"  Increased: {summary['increased']}")
    print(f"  Decreased: {summary['decreased']}")
    print(f"  Unchanged: {summary['unchanged']}")
    if "old_mean" in summary:
        print(f"  Mean score: {summary['old_mean']} → {summary['new_mean']}")


if __name__ == "__main__":
    main()
