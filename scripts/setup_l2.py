#!/usr/bin/env python3
"""Set up Polymarket L2 authentication.

This script:
1. Generates a fresh Polygon wallet (or uses POLYMARKET_PRIVATE_KEY from .env)
2. Derives CLOB API credentials via the py-clob-client SDK
3. Prints the values you need to add to your .env file

Usage::

    python scripts/setup_l2.py              # generate new wallet + derive creds
    python scripts/setup_l2.py --use-env    # derive creds from existing .env key
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def generate_wallet() -> tuple[str, str]:
    """Generate a fresh Polygon wallet.

    Returns:
        (private_key_hex, address)
    """
    from eth_account import Account

    acct = Account.create()
    return acct.key.hex(), acct.address


def derive_creds(private_key: str, host: str) -> dict:
    """Derive CLOB API credentials from a private key.

    Tries ``create_or_derive_api_creds()`` first (registers wallet + derives),
    then falls back to ``derive_api_key()`` if the wallet is already registered.

    Returns:
        Dict with api_key, api_secret, api_passphrase.
    """
    from py_clob_client.client import ClobClient

    client = ClobClient(host=host, key=private_key, chain_id=137)

    # Try create_or_derive first — this registers a fresh wallet with CLOB
    try:
        creds = client.create_or_derive_api_creds()
        return {
            "api_key": creds.api_key,
            "api_secret": creds.api_secret,
            "api_passphrase": creds.api_passphrase,
        }
    except Exception:
        pass

    # Fallback to derive_api_key for already-registered wallets
    creds = client.derive_api_key()
    return {
        "api_key": creds.api_key,
        "api_secret": creds.api_secret,
        "api_passphrase": creds.api_passphrase,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up Polymarket L2 auth")
    parser.add_argument(
        "--use-env",
        action="store_true",
        help="Use existing POLYMARKET_PRIVATE_KEY from .env instead of generating a new wallet",
    )
    parser.add_argument(
        "--host",
        default="https://clob.polymarket.com",
        help="CLOB API host URL",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  Penumbra — Polymarket L2 Auth Setup")
    print("=" * 60)

    if args.use_env:
        from sentinel.config import settings

        pk = settings.polymarket_private_key
        if not pk:
            print("\n[ERROR] POLYMARKET_PRIVATE_KEY not found in .env")
            print("Run without --use-env to generate a new wallet.\n")
            sys.exit(1)
        address = "(from .env)"
    else:
        print("\n[1/3] Generating fresh Polygon wallet...")
        pk, address = generate_wallet()
        print(f"  Address:     {address}")
        print(f"  Private Key: {pk[:10]}...{pk[-6:]}")

    print("\n[2/3] Deriving CLOB API credentials...")
    print(f"  Host: {args.host}")

    try:
        creds = derive_creds(pk, args.host)
    except Exception as exc:
        print(f"\n[ERROR] Derivation failed: {exc}")
        print("\nThis usually means:")
        print("  - VPN is not connected (Polymarket is geo-blocked outside US)")
        print("  - The CLOB API is temporarily down")
        print("\nMake sure your VPN is active and try again.\n")
        sys.exit(1)

    print("  API Key:        OK")
    print("  API Secret:     OK")
    print("  API Passphrase: OK")

    print("\n[3/3] Add these to your .env file:")
    print("-" * 60)

    if not args.use_env:
        print(f"POLYMARKET_PRIVATE_KEY={pk}")
    print(f"POLYMARKET_API_KEY={creds['api_key']}")
    print(f"POLYMARKET_API_SECRET={creds['api_secret']}")
    print(f"POLYMARKET_API_PASSPHRASE={creds['api_passphrase']}")

    print("-" * 60)
    print("\nDone! The backfill script will now use L2 auth automatically.")

    if not args.use_env:
        print(
            "\n[!] IMPORTANT: Save the private key above. "
            "If you lose it, you'll need to generate a new one."
        )


if __name__ == "__main__":
    main()
