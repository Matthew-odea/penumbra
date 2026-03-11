"""Backfill historical trades from the Polymarket CLOB API.

Usage::

    # Backfill all configured-category markets (7 days)
    python scripts/backfill.py --category Biotech --days 7

    # Backfill specific markets
    python scripts/backfill.py --markets 0xabc...,0xdef...

    # Dry run — print trades to stdout
    python scripts/backfill.py --category Biotech --dry-run

.. note::

    The ``/data/trades`` REST endpoint requires **Level-2 authentication**
    (a Polygon wallet private key).  Set ``POLYMARKET_PRIVATE_KEY`` in your
    ``.env`` file to enable backfill.  Without it, the script will exit
    gracefully and suggest using WebSocket accumulation instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import structlog

from sentinel.config import settings
from sentinel.db.init import init_schema
from sentinel.ingester.markets import fetch_all_markets
from sentinel.ingester.models import Trade, parse_rest_trade

logger = structlog.get_logger()

# CLOB trades endpoint (requires L2 auth, but we try with API key header)
_TRADES_PATH = "/data/trades"
_END_CURSOR = "LTE="
_RATE_LIMIT_DELAY = 0.65  # ~100 req/min → 600 ms between requests
_CURSOR_FILE = Path("data/.backfill_cursor.json")


def _save_cursor(market_id: str, cursor: str) -> None:
    """Persist cursor for resumable backfill."""
    _CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    state: dict = {}
    if _CURSOR_FILE.exists():
        state = json.loads(_CURSOR_FILE.read_text())
    state[market_id] = cursor
    _CURSOR_FILE.write_text(json.dumps(state, indent=2))


def _load_cursor(market_id: str) -> str | None:
    if _CURSOR_FILE.exists():
        state = json.loads(_CURSOR_FILE.read_text())
        return state.get(market_id)
    return None


async def backfill_market(
    market_id: str,
    *,
    conn: object | None = None,
    days: int = 7,
    dry_run: bool = False,
    api_key: str = "",
    api_secret: str = "",
    api_passphrase: str = "",
    base_url: str | None = None,
) -> int:
    """Backfill trades for a single market.

    Returns the number of trades ingested.
    """
    url = base_url or settings.polymarket_rest_url
    cutoff = datetime.now(UTC) - timedelta(days=days)
    cursor = _load_cursor(market_id) or "MA=="
    total = 0
    page = 0

    headers: dict[str, str] = {}
    if api_key:
        headers["POLY_API_KEY"] = api_key
        headers["POLY_API_SECRET"] = api_secret
        headers["POLY_PASSPHRASE"] = api_passphrase

    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        while cursor and cursor != _END_CURSOR:
            resp = await client.get(
                f"{url}{_TRADES_PATH}",
                params={"market": market_id, "next_cursor": cursor},
                headers=headers,
            )

            if resp.status_code == 401:
                logger.error(
                    "Backfill requires L2 authentication. "
                    "Set POLYMARKET_PRIVATE_KEY in .env or use WebSocket accumulation."
                )
                return total

            resp.raise_for_status()
            body = resp.json()
            page += 1

            trades: list[Trade] = []
            oldest_ts: datetime | None = None

            for raw in body.get("data", []):
                trade = parse_rest_trade(raw, market_id=market_id)
                if trade is None:
                    continue
                if trade.timestamp < cutoff:
                    oldest_ts = trade.timestamp
                    break
                trades.append(trade)
                oldest_ts = trade.timestamp

            if trades:
                if dry_run:
                    for t in trades:
                        print(json.dumps(t.as_dict(), indent=2))
                elif conn is not None:
                    rows = [t.as_db_tuple() for t in trades]
                    conn.executemany(
                        """INSERT OR IGNORE INTO trades
                           (trade_id, market_id, asset_id, wallet, side,
                            price, size_usd, timestamp, tx_hash)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        rows,
                    )
                total += len(trades)

            cursor = body.get("next_cursor")
            _save_cursor(market_id, cursor or _END_CURSOR)

            logger.info(
                "Backfill progress",
                market=market_id[:12] + "...",
                page=page,
                trades_loaded=total,
                oldest=oldest_ts.isoformat() if oldest_ts else "?",
            )

            # Check if we've reached the cutoff
            if oldest_ts and oldest_ts < cutoff:
                logger.info("Reached cutoff date", cutoff=cutoff.isoformat())
                break

            # Rate-limit
            await asyncio.sleep(_RATE_LIMIT_DELAY)

    logger.info("Backfill complete for market", market=market_id[:12] + "...", total=total)
    return total


async def run_backfill(
    *,
    market_ids: list[str] | None = None,
    category: str | None = None,
    days: int = 7,
    dry_run: bool = False,
) -> int:
    """Run backfill for multiple markets.

    If no ``market_ids`` are given, markets are fetched from the API and
    filtered by ``category``.
    """
    conn = None if dry_run else init_schema()

    if not market_ids:
        categories = [category] if category else settings.categories_list
        raw_markets = await fetch_all_markets(categories=categories)
        market_ids = [m["condition_id"] for m in raw_markets]
        logger.info("Backfilling markets from API", count=len(market_ids), categories=categories)

    if not market_ids:
        logger.warning("No markets found for backfill")
        return 0

    total = 0
    for i, mid in enumerate(market_ids, 1):
        logger.info(
            "Backfilling market",
            index=f"{i}/{len(market_ids)}",
            market=mid[:16] + "...",
        )
        count = await backfill_market(
            mid,
            conn=conn,
            days=days,
            dry_run=dry_run,
        )
        total += count

    if conn is not None:
        conn.close()

    logger.info("Backfill complete", total_trades=total, markets=len(market_ids))
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill Polymarket historical trades")
    parser.add_argument(
        "--markets",
        type=str,
        default="",
        help="Comma-separated condition_ids to backfill",
    )
    parser.add_argument(
        "--category",
        type=str,
        default="",
        help="Category filter (e.g. Biotech)",
    )
    parser.add_argument("--days", type=int, default=7, help="Number of days to backfill")
    parser.add_argument("--dry-run", action="store_true", help="Print trades without DB writes")
    args = parser.parse_args()

    market_ids = [m.strip() for m in args.markets.split(",") if m.strip()] if args.markets else None

    total = asyncio.run(
        run_backfill(
            market_ids=market_ids,
            category=args.category or None,
            days=args.days,
            dry_run=args.dry_run,
        )
    )
    print(f"\nBackfill complete: {total} trades loaded.")


if __name__ == "__main__":
    main()
