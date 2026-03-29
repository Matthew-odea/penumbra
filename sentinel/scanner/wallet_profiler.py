"""Wallet win-rate profiler.

Computes per-wallet performance on resolved markets using DuckDB's
``v_wallet_performance`` view and exposes lookup helpers for the
scanner pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from sentinel.config import settings

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class WalletProfile:
    """Aggregated performance for a single wallet."""

    wallet: str
    total_resolved_trades: int
    wins: int
    win_rate: float
    is_whitelisted: bool


# ── SQL ─────────────────────────────────────────────────────────────────────

_PROFILE_SQL = """
SELECT
    wallet,
    total_resolved_trades,
    wins,
    win_rate
FROM v_wallet_performance
WHERE wallet = ?
"""

_TOP_WALLETS_SQL = """
SELECT
    wallet,
    total_resolved_trades,
    wins,
    win_rate
FROM v_wallet_performance
WHERE total_resolved_trades >= ?
ORDER BY win_rate DESC
LIMIT ?
"""

_WHITELISTED_SQL = """
SELECT
    wallet,
    total_resolved_trades,
    wins,
    win_rate
FROM v_wallet_performance
WHERE win_rate >= ?
  AND total_resolved_trades >= ?
ORDER BY win_rate DESC
"""


def _row_to_profile(
    row: tuple,
    *,
    whitelist_rate: float | None = None,
    whitelist_min: int | None = None,
) -> WalletProfile:
    wl_rate = whitelist_rate if whitelist_rate is not None else settings.wallet_whitelist_win_rate
    wl_min = whitelist_min if whitelist_min is not None else settings.wallet_whitelist_min_trades
    wallet = str(row[0])
    total = int(row[1] or 0)
    wins = int(row[2] or 0)
    win_rate = float(row[3] or 0)
    is_wl = win_rate >= wl_rate and total >= wl_min
    return WalletProfile(
        wallet=wallet,
        total_resolved_trades=total,
        wins=wins,
        win_rate=win_rate,
        is_whitelisted=is_wl,
    )


_RESOLVED_TRADE_COUNT_SQL = """
SELECT COUNT(*)
FROM v_deduped_trades t
JOIN markets m ON t.market_id = m.market_id
WHERE t.wallet = ? AND m.resolved = TRUE
"""


def get_resolved_trade_count(conn: Any, wallet: str) -> int:
    """Return the total number of resolved trades for a wallet (no minimum threshold).

    Unlike ``get_wallet_profile`` (which returns None for < 5 trades), this
    always returns the actual count. Used to distinguish "truly new wallet"
    (0 trades) from "has some history but below the profiling threshold" (1-4).
    """
    row = conn.execute(_RESOLVED_TRADE_COUNT_SQL, [wallet]).fetchone()
    return int(row[0]) if row else 0


def get_wallet_profile(conn: Any, wallet: str) -> WalletProfile | None:
    """Look up a single wallet's performance on resolved markets.

    Returns ``None`` if the wallet has fewer than ``wallet_min_trades``
    resolved trades (view enforces HAVING COUNT(*) >= 5).
    """
    row = conn.execute(_PROFILE_SQL, [wallet]).fetchone()
    return _row_to_profile(row) if row else None


def get_whitelisted_wallets(conn: Any) -> list[WalletProfile]:
    """Return all wallets meeting the whitelist criteria.

    Defaults: win_rate ≥ 0.65 and ≥ 20 resolved trades.
    """
    rows = conn.execute(
        _WHITELISTED_SQL,
        [settings.wallet_whitelist_win_rate, settings.wallet_whitelist_min_trades],
    ).fetchall()
    return [_row_to_profile(r) for r in rows]


def get_top_wallets(
    conn: Any,
    *,
    min_trades: int | None = None,
    limit: int = 50,
) -> list[WalletProfile]:
    """Return top wallets by win rate with at least *min_trades* resolved trades."""
    min_t = min_trades if min_trades is not None else settings.wallet_min_trades
    rows = conn.execute(_TOP_WALLETS_SQL, [min_t, limit]).fetchall()
    return [_row_to_profile(r) for r in rows]


def is_whitelisted(conn: Any, wallet: str) -> bool:
    """Quick check whether a wallet meets whitelist criteria."""
    profile = get_wallet_profile(conn, wallet)
    return profile.is_whitelisted if profile else False
