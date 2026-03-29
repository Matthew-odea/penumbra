"""Kyle's Lambda — permanent price impact coefficient.

Kyle (1985): "Continuous Auctions and Insider Trading."

Estimates how much each dollar of net order flow moves the price,
via OLS regression of price changes on signed volume in 5-minute windows.
Uses DuckDB's native REGR_SLOPE / REGR_R2 — no external dependencies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from sentinel.config import settings

logger = structlog.get_logger()

# ── SQL ──────────────────────────────────────────────────────────────────────

_LAMBDA_OLS_SQL = """
WITH five_min AS (
    SELECT
        to_timestamp(FLOOR(epoch(timestamp) / 300) * 300) AS bucket,
        LAST(price ORDER BY timestamp) - FIRST(price ORDER BY timestamp) AS delta_price,
        SUM(CASE WHEN side = 'BUY' THEN size_usd ELSE -size_usd END) AS signed_volume
    FROM v_deduped_trades
    WHERE market_id = ?
      AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '{window_min} minutes'
    GROUP BY 1
    HAVING COUNT(DISTINCT timestamp) >= 2
)
SELECT
    REGR_SLOPE(delta_price, signed_volume) AS lambda,
    REGR_R2(delta_price, signed_volume) AS r_squared,
    COUNT(*) AS n
FROM five_min
WHERE signed_volume != 0
"""

_RESIDUAL_STD_SQL = """
WITH five_min AS (
    SELECT
        to_timestamp(FLOOR(epoch(timestamp) / 300) * 300) AS bucket,
        LAST(price ORDER BY timestamp) - FIRST(price ORDER BY timestamp) AS delta_price,
        SUM(CASE WHEN side = 'BUY' THEN size_usd ELSE -size_usd END) AS signed_volume
    FROM v_deduped_trades
    WHERE market_id = ?
      AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '{window_min} minutes'
    GROUP BY 1
    HAVING COUNT(DISTINCT timestamp) >= 2
)
SELECT STDDEV_POP(delta_price - (? * signed_volume))
FROM five_min
WHERE signed_volume != 0
"""

_INSERT_LAMBDA_SQL = """
INSERT INTO market_lambda (market_id, estimated_at, lambda_value, r_squared, residual_std, n_obs)
VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
"""

# ── Cache ────────────────────────────────────────────────────────────────────

_lambda_cache: dict[str, tuple[float, float, float, int, datetime]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes
_last_cleanup: datetime | None = None
_CLEANUP_INTERVAL_SECONDS = 3600  # Run retention cleanup at most once per hour

_CLEANUP_SQL = """
DELETE FROM market_lambda WHERE estimated_at < CURRENT_TIMESTAMP - INTERVAL '7 days'
"""


# ── Public API ───────────────────────────────────────────────────────────────


def estimate_lambda(conn: Any, market_id: str) -> tuple[float, float, float, int] | None:
    """Estimate Kyle's Lambda for a market using rolling OLS.

    Returns ``(lambda_val, r_squared, residual_std, n_obs)`` or ``None`` if
    insufficient data (fewer than ``lambda_min_observations`` five-minute
    windows with nonzero flow).

    The regression is::

        delta_price(t) = lambda * signed_volume(t) + epsilon

    where each observation is one 5-minute bucket.
    """
    window_min = settings.lambda_window_minutes
    min_obs = settings.lambda_min_observations

    ols_sql = _LAMBDA_OLS_SQL.format(window_min=window_min)
    row = conn.execute(ols_sql, [market_id]).fetchone()

    if not row or row[0] is None or row[2] < min_obs:
        return None

    import math

    lambda_val = float(row[0])
    if math.isnan(lambda_val) or math.isinf(lambda_val):
        return None
    r_squared = float(row[1]) if row[1] is not None else 0.0
    if math.isnan(r_squared):
        r_squared = 0.0
    n_obs = int(row[2])

    # Compute residual std in a second pass (avoids nested aggregate issues).
    # STDDEV_POP can overflow on very small DECIMAL residuals in DuckDB.
    residual_std = 0.0
    try:
        res_sql = _RESIDUAL_STD_SQL.format(window_min=window_min)
        res_row = conn.execute(res_sql, [market_id, lambda_val]).fetchone()
        if res_row and res_row[0] is not None:
            residual_std = float(res_row[0])
    except Exception as exc:
        # DuckDB raises OutOfRangeException (subclass of DataError) on STDDEV_POP
        # overflow with very small DECIMAL residuals. Log and default to 0.
        logger.debug("Residual std computation failed", market=market_id[:12], error=str(exc))

    logger.debug(
        "Lambda estimated",
        market=market_id[:12],
        lambda_val=round(lambda_val, 8),
        r_squared=round(r_squared, 4),
        residual_std=round(residual_std, 8),
        n_obs=n_obs,
    )
    return (lambda_val, r_squared, residual_std, n_obs)


def store_lambda(
    conn: Any,
    market_id: str,
    lambda_val: float,
    r_squared: float,
    residual_std: float,
    n_obs: int,
) -> None:
    """Persist a Lambda estimate to the ``market_lambda`` table."""
    conn.execute(_INSERT_LAMBDA_SQL, [market_id, lambda_val, r_squared, residual_std, n_obs])


def get_cached_lambda(conn: Any, market_id: str) -> tuple[float, float, float, int] | None:
    """Return a cached Lambda estimate, recomputing if stale (>5 min).

    On cache hit the DB is not touched.  On miss or expiry, calls
    ``estimate_lambda`` and persists the result via ``store_lambda``.
    """
    now = datetime.now(tz=UTC)
    if market_id in _lambda_cache:
        val, r2, std, n, ts = _lambda_cache[market_id]
        if (now - ts).total_seconds() < _CACHE_TTL_SECONDS:
            return (val, r2, std, n)

    result = estimate_lambda(conn, market_id)
    if result is not None:
        _lambda_cache[market_id] = (*result, now)
        store_lambda(conn, market_id, *result)

    # Lazy retention cleanup — at most once per hour
    global _last_cleanup
    if _last_cleanup is None or (now - _last_cleanup).total_seconds() > _CLEANUP_INTERVAL_SECONDS:
        try:
            conn.execute(_CLEANUP_SQL)
            _last_cleanup = now
        except Exception:
            pass

    return result
