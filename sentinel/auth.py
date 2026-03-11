"""Polymarket CLOB L2 authentication helper.

The Polymarket CLOB API requires Level-2 auth (a Polygon wallet signature)
for endpoints like ``/data/trades``.  This module:

1. Takes a private key from ``settings.polymarket_private_key``
2. Derives CLOB API credentials (key, secret, passphrase) via the SDK
3. Caches derived creds so we only derive once per session

Usage::

    from sentinel.auth import get_clob_headers

    headers = get_clob_headers()
    # → {"POLY_API_KEY": "...", "POLY_API_SECRET": "...", "POLY_PASSPHRASE": "..."}
"""

from __future__ import annotations

import structlog

from sentinel.config import settings

logger = structlog.get_logger()

# In-memory cache — derived once, reused for the process lifetime.
_cached_headers: dict[str, str] | None = None


def has_l2_auth() -> bool:
    """Return True if a private key is configured for L2 auth."""
    return bool(settings.polymarket_private_key)


def derive_api_creds() -> dict[str, str]:
    """Derive CLOB API credentials from the configured private key.

    Returns:
        Dict with keys ``POLY_API_KEY``, ``POLY_API_SECRET``, ``POLY_PASSPHRASE``.

    Raises:
        RuntimeError: If no private key is configured or derivation fails.
    """
    pk = settings.polymarket_private_key
    if not pk:
        raise RuntimeError(
            "POLYMARKET_PRIVATE_KEY not set in .env — run `python scripts/setup_l2.py` first"
        )

    try:
        from py_clob_client.client import ClobClient

        # Chain ID 137 = Polygon mainnet
        client = ClobClient(
            host=settings.polymarket_rest_url,
            key=pk,
            chain_id=137,
        )
        # Try create_or_derive first (registers fresh wallets with CLOB)
        try:
            creds = client.create_or_derive_api_creds()
        except Exception:
            creds = client.derive_api_key()

        logger.info("CLOB API credentials derived successfully")

        return {
            "POLY_API_KEY": creds.api_key,
            "POLY_API_SECRET": creds.api_secret,
            "POLY_PASSPHRASE": creds.api_passphrase,
        }
    except Exception as exc:
        raise RuntimeError(f"Failed to derive CLOB API creds: {exc}") from exc


def get_clob_headers() -> dict[str, str]:
    """Get CLOB auth headers, deriving on first call.

    Returns an empty dict if no private key is configured (graceful fallback).
    """
    global _cached_headers

    if _cached_headers is not None:
        return _cached_headers

    if not has_l2_auth():
        logger.debug("No L2 auth configured — using public endpoints only")
        return {}

    # Check if pre-derived creds exist in config
    if settings.polymarket_api_key and settings.polymarket_api_secret:
        _cached_headers = {
            "POLY_API_KEY": settings.polymarket_api_key,
            "POLY_API_SECRET": settings.polymarket_api_secret,
            "POLY_PASSPHRASE": settings.polymarket_api_passphrase,
        }
        logger.info("Using pre-derived CLOB API credentials from .env")
        return _cached_headers

    # Derive on-the-fly
    _cached_headers = derive_api_creds()
    return _cached_headers
