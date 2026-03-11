"""WebSocket listener for live Polymarket order-book events.

Connects to the Polymarket CLOB WebSocket, subscribes to the requested
``asset_ids`` (token IDs), parses incoming messages into typed events
(:class:`Trade` and :class:`BookEvent`), and dispatches them via callbacks.

The WS feed at ``/ws/market`` delivers:

* **Book snapshots** (initial ``bids`` / ``asks``) — logged but not forwarded.
* **Price changes** (``price_changes``) — parsed into :class:`BookEvent` and
  forwarded via *on_book_event*.  These are the primary real-time signal for
  informed-flow detection.
* **Trade executions** (``event_type == "trade"``) — parsed into :class:`Trade`
  and forwarded via *on_trade*.
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
from sentinel.ingester.models import BookEvent, Trade, parse_price_changes, parse_ws_trade

logger = structlog.get_logger()

# Type aliases for the callbacks the listener invokes.
TradeCallback = Callable[[Trade], Coroutine[Any, Any, None]]
BookEventCallback = Callable[[BookEvent], Coroutine[Any, Any, None]]

# Reconnection parameters
_INITIAL_BACKOFF = 1.0  # seconds
_MAX_BACKOFF = 60.0
_MAX_RETRIES = 50  # effectively unlimited with backoff

# Lenient SSL context — Polymarket CDN cert can mismatch behind VPN/geo-fence
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


class Listener:
    """Async WebSocket listener for Polymarket market events.

    Usage::

        listener = Listener(
            on_trade=my_trade_cb,
            on_book_event=my_book_cb,
            asset_ids=["1154620873...", "4884925241..."],
        )
        await listener.run()          # blocks until cancelled
    """

    def __init__(
        self,
        *,
        on_trade: TradeCallback,
        on_book_event: BookEventCallback | None = None,
        asset_ids: list[str] | None = None,
        ws_url: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self._on_trade = on_trade
        self._on_book_event = on_book_event
        self._asset_ids = asset_ids or []
        self._ws_url = ws_url or settings.polymarket_ws_url
        self._dry_run = dry_run
        self._trade_count = 0
        self._book_event_count = 0
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

    @property
    def book_event_count(self) -> int:
        return self._book_event_count

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
                assets=len(self._asset_ids) or "none",
            )

            async for raw in ws:
                if not self._running:
                    break
                await self._handle_message(raw)

    async def _subscribe(self, ws: ClientConnection) -> None:
        """Send subscription message for the configured asset IDs.

        Polymarket WS requires the ``assets_ids`` format::

            {"auth": {}, "type": "subscribe", "markets": [], "assets_ids": [...]}
        """
        payload = {
            "auth": {},
            "type": "subscribe",
            "markets": [],
            "assets_ids": self._asset_ids,
        }
        await ws.send(json.dumps(payload))
        logger.debug(
            "Subscription sent",
            num_assets=len(self._asset_ids),
        )

    async def _handle_message(self, raw: str | bytes) -> None:
        """Parse a raw WS frame and dispatch as Trade or BookEvent."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Non-JSON WS message", raw=str(raw)[:200])
            return

        # Empty list is a subscription acknowledgement
        if isinstance(msg, list):
            if len(msg) == 0:
                logger.debug("WS subscription acknowledged")
            else:
                # Batch of messages — process each
                for m in msg:
                    await self._dispatch(m)
            return

        if isinstance(msg, dict):
            await self._dispatch(msg)

    async def _dispatch(self, msg: dict) -> None:
        """Route a single message dict to the appropriate handler."""
        # ── 1. Trade execution ──────────────────────────────────────────
        trade = parse_ws_trade(msg)
        if trade is not None:
            self._trade_count += 1
            if self._trade_count % 500 == 0:
                logger.info("Trade milestone", count=self._trade_count)
            await self._on_trade(trade)
            return

        # ── 2. Price changes (order book updates) ──────────────────────
        if "price_changes" in msg:
            events = parse_price_changes(msg)
            for evt in events:
                self._book_event_count += 1
                if self._on_book_event is not None:
                    await self._on_book_event(evt)
            if self._book_event_count % 500 == 0 and self._book_event_count > 0:
                logger.info("Book-event milestone", count=self._book_event_count)
            return

        # ── 3. Book snapshot (bids/asks) — log only ────────────────────
        if "bids" in msg or "asks" in msg:
            asset = msg.get("asset_id", "?")[:20]
            logger.debug("Book snapshot received", asset=asset)
            return

        # ── 4. Unknown ─────────────────────────────────────────────────
        logger.debug("Unhandled WS message", keys=list(msg.keys())[:5])
