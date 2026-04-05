"""On-chain trade poller — reads Polymarket OrdersMatched events from Polygon.

Polls ``eth_getLogs`` on the Alchemy RPC every N seconds to fetch settled
trade events directly from the Polymarket CTF Exchange contracts.  This
provides wallet-attributed trades within ~10 seconds, bypassing the 5-minute
CDN cache on the Polymarket REST data-api.

Cost model (Alchemy):
  - ``eth_getLogs`` = 75 CU per call regardless of result size.
  - At 10s interval: ~8,640 calls/day = 648k CU/day → fits in free tier (30M/mo).
  - At 4s interval: ~21,600 calls/day = 1.6M CU/day → requires PAYG (~$22/mo).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import structlog
from eth_abi import decode  # type: ignore[import-untyped]

from sentinel.config import settings
from sentinel.ingester.models import Trade

logger = structlog.get_logger()

TradeCallback = Callable[[Trade], Coroutine[Any, Any, None]]

# ── Polymarket contract addresses (Polygon mainnet) ─────────────────────────
_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
_NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# OrdersMatched(bytes32 indexed takerOrderHash, address indexed takerOrderMaker,
#               uint256 makerAssetId, uint256 takerAssetId,
#               uint256 makerAmountFilled, uint256 takerAmountFilled)
_ORDERS_MATCHED_TOPIC = "0x63bf4d16b7fa898ef4c4b2b6d90fd201e9c56313b65638af6088d149d2ce956c"

# Skip trades where taker is the exchange contract itself (NegRisk internal routing)
_EXCHANGE_ADDRESSES = {_CTF_EXCHANGE.lower(), _NEG_RISK_EXCHANGE.lower()}

# USDC.e has 6 decimals; CTF outcome tokens also use 6 decimals on Polymarket
_DECIMALS = 1e6

# Free tier: max 10 block range.  PAYG: up to 2000.
_FREE_TIER_MAX_RANGE = 10
_PAYG_MAX_RANGE = 2000


class ChainPoller:
    """Async poller that reads OrdersMatched events from Polygon via eth_getLogs.

    Args:
        on_trade: Callback for each decoded trade (same interface as WS/REST).
        token_map: Dict mapping ``token_id_str`` → ``condition_id``.
        rpc_url: Alchemy (or other) Polygon JSON-RPC URL.
        poll_interval: Seconds between polls.
        max_block_range: Max blocks per eth_getLogs call (10 for free tier).
    """

    def __init__(
        self,
        *,
        on_trade: TradeCallback,
        token_map: dict[str, str],
        rpc_url: str | None = None,
        poll_interval: int | None = None,
        max_block_range: int = _FREE_TIER_MAX_RANGE,
    ) -> None:
        self._on_trade = on_trade
        self._token_map = token_map
        self._rpc_url = rpc_url or settings.effective_alchemy_url
        self._poll_interval = poll_interval or settings.chain_poll_interval_seconds
        self._max_block_range = max_block_range
        self._last_block: int | None = None
        self._running = False
        self._trade_count = 0
        self._poll_count = 0
        self._decode_errors = 0

    # ── Public API ──────────────────────────────────────────────────────

    @property
    def trade_count(self) -> int:
        return self._trade_count

    @property
    def poll_count(self) -> int:
        return self._poll_count

    def update_token_map(self, token_map: dict[str, str]) -> None:
        """Hot-swap the token_id → condition_id lookup (called on market sync)."""
        self._token_map = token_map
        logger.info("Chain poller token map updated", tokens=len(token_map))

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Main loop — poll eth_getLogs at the configured interval."""
        self._running = True
        logger.info(
            "Chain poller starting",
            rpc_url=self._rpc_url[:40] + "...",
            interval_s=self._poll_interval,
            max_block_range=self._max_block_range,
            tokens=len(self._token_map),
        )

        backoff = 1.0
        while self._running:
            try:
                await self._poll_once()
                backoff = 1.0  # reset on success
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "Chain poll failed",
                    error=str(exc)[:120],
                    backoff_s=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            await asyncio.sleep(self._poll_interval)

    # ── Internals ───────────────────────────────────────────────────────

    async def _poll_once(self) -> None:
        """Fetch the latest block, request logs, decode and dispatch."""
        async with httpx.AsyncClient(timeout=15) as client:
            # Get current block number
            resp = await client.post(
                self._rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
            )
            resp.raise_for_status()
            current_block = int(resp.json()["result"], 16)

            if self._last_block is None:
                # First poll — start from current block (don't backfill history)
                self._last_block = current_block
                logger.info("Chain poller initialized", start_block=current_block)
                return

            if current_block <= self._last_block:
                return  # No new blocks

            # Clamp range to max_block_range
            from_block = self._last_block + 1
            to_block = min(current_block, from_block + self._max_block_range - 1)

            if from_block > to_block:
                return

            # Fetch OrdersMatched logs from both CTF exchange contracts
            resp = await client.post(
                self._rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "eth_getLogs",
                    "params": [{
                        "address": [_CTF_EXCHANGE, _NEG_RISK_EXCHANGE],
                        "topics": [_ORDERS_MATCHED_TOPIC],
                        "fromBlock": hex(from_block),
                        "toBlock": hex(to_block),
                    }],
                },
            )
            resp.raise_for_status()
            result = resp.json()

            if "error" in result:
                raise RuntimeError(result["error"].get("message", str(result["error"])))

            logs = result.get("result", [])
            self._poll_count += 1

            new_trades = 0
            for log in logs:
                trade = self._decode_log(log)
                if trade is not None:
                    self._trade_count += 1
                    new_trades += 1
                    await self._on_trade(trade)

            self._last_block = to_block

            if new_trades > 0 or self._poll_count % 60 == 0:
                logger.info(
                    "chain_poll",
                    blocks=f"{from_block}-{to_block}",
                    logs=len(logs),
                    matched=new_trades,
                    total=self._trade_count,
                )

            # If we're behind, immediately poll the next chunk
            if to_block < current_block:
                logger.debug(
                    "Chain poller catching up",
                    behind=current_block - to_block,
                )

    def _decode_log(self, log: dict[str, Any]) -> Trade | None:
        """Decode an OrdersMatched log into a Trade, or None if filtered out."""
        try:
            topics = log["topics"]
            raw_data = bytes.fromhex(log["data"][2:])

            # topics[2] = indexed takerOrderMaker (address, zero-padded to 32 bytes)
            taker_address = "0x" + topics[2][-40:]

            # Skip trades where taker is the exchange contract itself
            if taker_address.lower() in _EXCHANGE_ADDRESSES:
                return None

            # data = (uint256 makerAssetId, uint256 takerAssetId,
            #         uint256 makerAmountFilled, uint256 takerAmountFilled)
            maker_asset, taker_asset, maker_amt, taker_amt = decode(
                ["uint256", "uint256", "uint256", "uint256"], raw_data
            )

            # Determine side: assetId=0 means USDC (collateral)
            if taker_asset == 0:
                # Taker pays USDC, receives tokens → BUY
                token_id = str(maker_asset)
                side = "BUY"
                usdc = taker_amt / _DECIMALS
                tokens = maker_amt / _DECIMALS
            elif maker_asset == 0:
                # Taker pays tokens, receives USDC → SELL
                token_id = str(taker_asset)
                side = "SELL"
                usdc = maker_amt / _DECIMALS
                tokens = taker_amt / _DECIMALS
            else:
                # Token-for-token swap (rare arbitrage), skip
                return None

            # Filter: only process trades for markets we're tracking
            condition_id = self._token_map.get(token_id)
            if condition_id is None:
                return None

            price = usdc / tokens if tokens > 0 else 0.0

            tx_hash = log["transactionHash"]
            log_index = int(log["logIndex"], 16)

            # Derive timestamp from block (Polygon ~2s blocks, approximate)
            # For exact timestamps we'd need eth_getBlockByNumber, but that's
            # another 16 CU per call. Use current time as close approximation
            # since we're polling near-tip blocks.
            ts = datetime.now(tz=UTC)

            return Trade(
                trade_id=f"chain-{tx_hash}-{log_index}",
                market_id=condition_id,
                asset_id=token_id,
                wallet=taker_address,
                side=side,
                price=Decimal(str(round(price, 6))),
                size_usd=Decimal(str(round(usdc, 6))),
                timestamp=ts,
                tx_hash=tx_hash,
                source="chain",
            )
        except (KeyError, IndexError, ValueError, TypeError, InvalidOperation) as exc:
            self._decode_errors += 1
            if self._decode_errors <= 5:
                logger.debug("Chain event decode failed", error=str(exc))
            return None


def build_token_map(conn: Any) -> dict[str, str]:
    """Build a token_id → condition_id lookup dict from the markets table.

    Each market has a comma-separated ``token_ids`` field (e.g. "123,456")
    with the YES and NO token IDs.  This creates a dict mapping each
    individual token ID string to its parent condition_id.
    """
    rows = conn.execute(
        "SELECT market_id, token_ids FROM markets WHERE token_ids IS NOT NULL AND token_ids != ''"
    ).fetchall()

    token_map: dict[str, str] = {}
    for market_id, token_ids_str in rows:
        for tid in token_ids_str.split(","):
            tid = tid.strip()
            if tid:
                token_map[tid] = market_id

    logger.info("Token map built", markets=len(rows), tokens=len(token_map))
    return token_map
