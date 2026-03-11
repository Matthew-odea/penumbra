"""Scanner entry point — run as ``python -m sentinel.scanner``.

Modes:

    --backtest    Process all trades already in DuckDB through the scanner
    --live        Consume from the ingester queue (used when run as part
                  of the full Penumbra pipeline)
    --dry-run     Print signals to stdout instead of persisting to DuckDB

Example:

    # Process historical trades in the DB
    python -m sentinel.scanner --backtest --dry-run

    # Show volume anomalies only (diagnostic)
    python -m sentinel.scanner --anomalies
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

import structlog

from sentinel.config import settings
from sentinel.db.init import init_schema
from sentinel.ingester.models import Trade
from sentinel.scanner.funding import check_funding_anomaly
from sentinel.scanner.price_impact import get_high_impact_trades
from sentinel.scanner.scorer import Signal, build_signal, write_signal
from sentinel.scanner.volume import get_anomalies, get_zscore_for_market
from sentinel.scanner.wallet_profiler import get_wallet_profile, get_whitelisted_wallets

logger = structlog.get_logger()


# ── Backtest mode ───────────────────────────────────────────────────────────

_RECENT_TRADES_SQL = """
SELECT
    trade_id, market_id, asset_id, wallet, side,
    price, size_usd, timestamp, tx_hash
FROM trades
WHERE timestamp >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
  AND size_usd >= ?
ORDER BY timestamp DESC
"""


async def _backtest(conn: Any, *, dry_run: bool = False) -> int:
    """Run all recent trades through the scanner pipeline.

    Returns the number of signals emitted.
    """
    min_size = settings.min_trade_size_usd
    rows = conn.execute(_RECENT_TRADES_SQL, [min_size]).fetchall()

    if not rows:
        logger.info("No trades to backtest", min_size_usd=min_size)
        return 0

    logger.info("Backtest starting", trades=len(rows), min_size_usd=min_size)
    signals_emitted = 0

    for row in rows:
        trade_id = str(row[0])
        market_id = str(row[1])
        wallet = str(row[3])
        side = str(row[4])
        price = float(row[5] or 0)
        size_usd = float(row[6] or 0)
        trade_ts = row[7]

        # 1. Volume Z-score
        z_score = 0.0
        try:
            z_score = get_zscore_for_market(conn, market_id)
        except Exception:
            pass

        # 2. Price impact
        price_impact_score = 0.0
        try:
            from sentinel.scanner.price_impact import get_price_impact
            impact = get_price_impact(conn, market_id, trade_id)
            if impact:
                price_impact_score = impact.impact_score
        except Exception:
            pass

        # 3. Wallet profiling
        wallet_win_rate: float | None = None
        wallet_total_trades: int | None = None
        is_whitelisted = False
        try:
            profile = get_wallet_profile(conn, wallet)
            if profile:
                wallet_win_rate = profile.win_rate
                wallet_total_trades = profile.total_resolved_trades
                is_whitelisted = profile.is_whitelisted
        except Exception:
            pass

        # Pre-check
        has_signal = (
            z_score > settings.zscore_threshold
            or price_impact_score > 0.01
            or is_whitelisted
            or (wallet_win_rate is not None and wallet_win_rate > 0.6)
        )
        if not has_signal:
            continue

        # 4. Funding anomaly
        funding_anomaly = False
        funding_age_minutes: int | None = None
        try:
            funding = await check_funding_anomaly(wallet, trade_ts)
            funding_anomaly = funding.is_anomaly
            funding_age_minutes = funding.funding_age_minutes
        except Exception:
            pass

        signal = build_signal(
            trade_id=trade_id,
            market_id=market_id,
            wallet=wallet,
            side=side,
            price=price,
            size_usd=size_usd,
            trade_timestamp=trade_ts,
            z_score=z_score,
            modified_z_score=z_score,
            price_impact=price_impact_score,
            wallet_win_rate=wallet_win_rate,
            wallet_total_trades=wallet_total_trades,
            is_whitelisted=is_whitelisted,
            funding_anomaly=funding_anomaly,
            funding_age_minutes=funding_age_minutes,
        )

        if signal.statistical_score < settings.signal_min_score:
            continue

        signals_emitted += 1

        logger.info(
            "SIGNAL DETECTED",
            score=signal.statistical_score,
            market=market_id[:12],
            wallet=wallet[:10],
            trade=trade_id[:10],
        )

        if dry_run:
            print(json.dumps(signal.as_dict(), default=str))
        else:
            try:
                write_signal(conn, signal)
            except Exception as exc:
                logger.error("Failed to write signal", error=str(exc))

    logger.info("Backtest complete", signals=signals_emitted, trades_processed=len(rows))
    return signals_emitted


# ── Anomaly diagnostics ────────────────────────────────────────────────────


def _show_anomalies(conn: Any) -> None:
    """Print current volume anomalies (debugging / diagnostic)."""
    anomalies = get_anomalies(conn, threshold=0)  # Show all, not just above threshold
    if not anomalies:
        print("No volume data in the last 24 hours.")
        return

    print(f"\n{'Market':<16} {'Hour':<20} {'Volume $':<12} {'Trades':<8} {'Z-Score':<10} {'Flag'}")
    print("─" * 80)
    for a in anomalies:
        flag = "⚠️ " if a.modified_z_score >= settings.zscore_threshold else "  "
        print(
            f"{a.market_id[:14]:<16} "
            f"{str(a.hour_bucket):<20} "
            f"${a.volume_usd:<11,.0f} "
            f"{a.trade_count:<8} "
            f"{a.modified_z_score:<10.2f} "
            f"{flag}"
        )

    above = [a for a in anomalies if a.modified_z_score >= settings.zscore_threshold]
    print(f"\n{len(above)} / {len(anomalies)} buckets above threshold ({settings.zscore_threshold})")


def _show_wallets(conn: Any) -> None:
    """Print whitelisted wallets."""
    wallets = get_whitelisted_wallets(conn)
    if not wallets:
        print("No whitelisted wallets yet (need resolved markets + trades).")
        return

    print(f"\n{'Wallet':<14} {'Trades':<10} {'Wins':<8} {'Win Rate':<10} {'Whitelisted'}")
    print("─" * 56)
    for w in wallets:
        print(
            f"{w.wallet[:12]:<14} "
            f"{w.total_resolved_trades:<10} "
            f"{w.wins:<8} "
            f"{w.win_rate:<10.1%} "
            f"{'✓' if w.is_whitelisted else ''}"
        )


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Penumbra scanner — statistical signal engine")
    parser.add_argument("--backtest", action="store_true", help="Process trades already in DuckDB")
    parser.add_argument("--anomalies", action="store_true", help="Show current volume anomalies")
    parser.add_argument("--wallets", action="store_true", help="Show whitelisted wallets")
    parser.add_argument("--dry-run", action="store_true", help="Print signals, don't persist")
    args = parser.parse_args()

    conn = init_schema()

    try:
        if args.anomalies:
            _show_anomalies(conn)
        elif args.wallets:
            _show_wallets(conn)
        elif args.backtest:
            count = asyncio.run(_backtest(conn, dry_run=args.dry_run))
            print(f"\nBacktest finished — {count} signal(s) emitted.")
        else:
            parser.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
