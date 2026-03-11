"""WebSocket listener for live Polymarket trades.

Connects to the Polymarket CLOB WebSocket, subscribes to markets of interest,
parses incoming trade messages, and pushes them to the batch buffer.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any, Callable, Coroutine

import structlog
import websockets
import websockets.asyncio.client as ws_client
from websockets.asyncio.client import ClientConnection

from sentinel.config import settings
from sentinel.ingester.models import Trade, parse_ws_trade

logger = structlog.get_logger()

# Type alias for the callback the listener invokes for each parsed trade.
TradeCallback = Callable[[Trade], Coroutine[Any, Any, None]]

# Reconnection parameters
_INITIAL_BACKOFF = 1.0  # seconds
_MAX_BACKOFF = 60.0
_MAX_RETRIES = 50  # effectively unlimited with backoff

# Lenient SSL context — Polymarket CDN cert can mismatch behind VPN/geo-fence
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


class Listener:
    """Async WebSocket listener for Polymarket trade events.

    Usage::

        listener = Listener(on_trade=my_callback, market_ids=["0x..."])
        await listener.run()          # blocks until cancelled
    """

    def __init__(
        self,
        *,
        on_trade: TradeCallback,
        market_ids: list[str] | None = None,
        ws_url: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self._on_trade = on_trade
        self._market_ids = market_ids or []
        self._ws_url = ws_url or settings.polymarket_ws_url
        self._dry_run = dry_run
        self._trade_count = 0
        self._running = False

    # ── public API ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect and listen in a loop with exponential backoff."""
        self._running = True
        backoff = _INITIAL_BACKOFF
        retries = 0

        while self._running and retries < _MAX_RETRIES:
            try:
                await self._connect_and_listen()
                # Clean exit (e.g. cancelled) — don't retry.
                break
            except asyncio.CancelledError:
                logger.info("Listener cancelled")
                break
            except Exception as exc:
                retries += 1
                logger.warning(
                    "WS disconnected — reconnecting",
                    error=str(exc),
                    retry=retries,
                    backoff_s=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

        if retries >= _MAX_RETRIES:
            logger.error("Max WS retries exceeded — giving up")

    def stop(self) -> None:
        """Signal the listener to stop after the current iteration."""
        self._running = False

    @property
    def trade_count(self) -> int:
        return self._trade_count

    # ── internals ───────────────────────────────────────────────────────

    async def _connect_and_listen(self) -> None:
        """Open WS, subscribe, and dispatch messages."""
        logger.info("Connecting to Polymarket WS", url=self._ws_url)

        # websockets 13+ async API
        async with ws_client.connect(
            self._ws_url,
            additional_headers={"Origin": "https://polymarket.com"},
            ssl=_SSL_CTX,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        ) as ws:
            await self._subscribe(ws)
            logger.info(
                "WS connected and subscribed",
                markets=len(self._market_ids) or "all",
            )

            async for raw in ws:
                if not self._running:
                    break
                await self._handle_message(raw)

    async def _subscribe(self, ws: ClientConnection) -> None:
        """Send subscription messages for the configured markets."""
        if self._market_ids:
            # Subscribe to each market individually
            for mid in self._market_ids:
                payload = {
                    "type": "subscribe",
                    "channel": "market",
                    "market": mid,
                }
                await ws.send(json.dumps(payload))
                logger.debug("Subscribed to market", market_id=mid)
        else:
            # Subscribe to the global trade feed
            payload = {"type": "subscribe", "channel": "market"}
            await ws.send(json.dumps(payload))
            logger.debug("Subscribed to global trade feed")

    async def _handle_message(self, raw: str | bytes) -> None:
        """Parse a raw WS frame and dispatch the trade."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Non-JSON WS message", raw=str(raw)[:200])
            return

        # Log raw messages at debug level for diagnostics
        if isinstance(msg, dict):
            etype = msg.get("event_type") or msg.get("type") or "unknown"
            logger.debug("WS message received", event_type=etype)
        elif isinstance(msg, list):
            logger.debug("WS batch message", count=len(msg))

        # Polymarket sometimes wraps events in a list
        messages = msg if isinstance(msg, list) else [msg]

        for m in messages:
            trade = parse_ws_trade(m)
            if trade is None:
                continue

            self._trade_count += 1
            if self._trade_count % 500 == 0:
                logger.info("Trade milestone", count=self._trade_count)

            await self._on_trade(trade)
