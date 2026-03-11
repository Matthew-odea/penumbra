"""Funding anomaly checker via Alchemy Transfers API.

Checks whether a wallet was recently funded (< 60 min before a flagged trade).
Fresh wallets + large trades = classic informed-flow pattern.

Only called for wallets already flagged by volume or price-impact filters to
keep API usage minimal (Alchemy free tier: 300 CU/s).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from sentinel.config import settings

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class FundingResult:
    """Result of a funding-recency check for a single wallet."""

    wallet: str
    is_anomaly: bool
    funding_age_minutes: int | None  # None = no funding found / API error
    first_funding_tx: str | None
    checked_at: datetime


# ── In-memory cache (wallet → FundingResult, TTL 1h) ───────────────────────

_cache: dict[str, tuple[float, FundingResult]] = {}
_CACHE_TTL = 3600  # seconds


def _get_cached(wallet: str) -> FundingResult | None:
    entry = _cache.get(wallet)
    if entry is None:
        return None
    ts, result = entry
    if time.monotonic() - ts > _CACHE_TTL:
        del _cache[wallet]
        return None
    return result


def _set_cached(wallet: str, result: FundingResult) -> None:
    _cache[wallet] = (time.monotonic(), result)


def clear_cache() -> None:
    """Clear the funding result cache (useful for testing)."""
    _cache.clear()


# ── Alchemy Transfers API ──────────────────────────────────────────────────

_ALCHEMY_TIMEOUT = 10  # seconds


async def check_funding_anomaly(
    wallet: str,
    trade_timestamp: datetime,
    *,
    threshold_minutes: int | None = None,
) -> FundingResult:
    """Check if *wallet* was funded within *threshold_minutes* of a trade.

    Uses the Alchemy ``alchemy_getAssetTransfers`` endpoint to look up
    incoming MATIC / USDC transfers to the wallet.

    Falls back gracefully (logs warning, returns non-anomaly) if Alchemy is
    unavailable or the API key is not set.
    """
    cached = _get_cached(wallet)
    if cached is not None:
        return cached

    threshold = threshold_minutes or settings.funding_anomaly_threshold_minutes
    alchemy_url = settings.effective_alchemy_url

    if not settings.alchemy_api_key:
        logger.debug("Alchemy API key not configured — skipping funding check", wallet=wallet[:10])
        result = FundingResult(
            wallet=wallet,
            is_anomaly=False,
            funding_age_minutes=None,
            first_funding_tx=None,
            checked_at=datetime.now(tz=UTC),
        )
        _set_cached(wallet, result)
        return result

    try:
        result = await _query_alchemy(wallet, trade_timestamp, alchemy_url, threshold)
    except Exception as exc:
        logger.warning(
            "Alchemy funding check failed — skipping",
            wallet=wallet[:10],
            error=str(exc),
        )
        result = FundingResult(
            wallet=wallet,
            is_anomaly=False,
            funding_age_minutes=None,
            first_funding_tx=None,
            checked_at=datetime.now(tz=UTC),
        )

    _set_cached(wallet, result)
    return result


async def _query_alchemy(
    wallet: str,
    trade_timestamp: datetime,
    alchemy_url: str,
    threshold_minutes: int,
) -> FundingResult:
    """Raw Alchemy RPC call to get incoming transfers."""
    payload = {
        "id": 1,
        "jsonrpc": "2.0",
        "method": "alchemy_getAssetTransfers",
        "params": [
            {
                "fromBlock": "0x0",
                "toBlock": "latest",
                "toAddress": wallet,
                "category": ["external", "erc20"],
                "order": "asc",
                "maxCount": "0x5",  # Only need the first few
                "withMetadata": True,
            }
        ],
    }

    async with httpx.AsyncClient(timeout=_ALCHEMY_TIMEOUT, verify=False) as client:
        resp = await client.post(alchemy_url, json=payload)
        resp.raise_for_status()
        data = resp.json()

    transfers = data.get("result", {}).get("transfers", [])

    if not transfers:
        return FundingResult(
            wallet=wallet,
            is_anomaly=False,
            funding_age_minutes=None,
            first_funding_tx=None,
            checked_at=datetime.now(tz=UTC),
        )

    # Find the earliest transfer
    first_tx = transfers[0]
    tx_hash = first_tx.get("hash", "")

    # Parse block timestamp from metadata
    metadata = first_tx.get("metadata", {})
    block_ts_str = metadata.get("blockTimestamp", "")

    if block_ts_str:
        # Alchemy returns ISO 8601 timestamps
        funding_time = datetime.fromisoformat(block_ts_str.replace("Z", "+00:00"))
    else:
        # Fallback: can't determine time → not anomaly
        return FundingResult(
            wallet=wallet,
            is_anomaly=False,
            funding_age_minutes=None,
            first_funding_tx=tx_hash,
            checked_at=datetime.now(tz=UTC),
        )

    age = trade_timestamp - funding_time
    age_minutes = max(0, int(age.total_seconds() / 60))

    is_anomaly = age_minutes < threshold_minutes

    if is_anomaly:
        logger.info(
            "Funding anomaly detected",
            wallet=wallet[:10],
            age_minutes=age_minutes,
            threshold=threshold_minutes,
        )

    return FundingResult(
        wallet=wallet,
        is_anomaly=is_anomaly,
        funding_age_minutes=age_minutes,
        first_funding_tx=tx_hash,
        checked_at=datetime.now(tz=UTC),
    )
