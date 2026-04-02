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
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
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
_MAX_BACKOFF = 15.0     # reduced from 60s — longer gaps lose more trades

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
        on_reconnect: Callable[[], Coroutine[Any, Any, None]] | None = None,
        asset_ids: list[str] | None = None,
        ws_url: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self._on_trade = on_trade
        self._on_book_event = on_book_event
        self._on_reconnect = on_reconnect
        self._asset_ids = list(asset_ids or [])
        self._ws_url = ws_url or settings.polymarket_ws_url
        self._dry_run = dry_run
        self._trade_count = 0
        self._book_event_count = 0
        self._book_message_count = 0
        self._reconnect_count = 0
        self._running = False
        self._ws: ClientConnection | None = None  # active connection, None between reconnects

    # ── public API ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect and listen in a loop with exponential backoff."""
        self._running = True
        backoff = _INITIAL_BACKOFF

        while self._running:
            connected_at = asyncio.get_event_loop().time()
            try:
                await self._connect_and_listen()
                # Clean exit (e.g. cancelled) — don't retry.
                break
            except asyncio.CancelledError:
                logger.info("Listener cancelled")
                break
            except Exception as exc:
                self._reconnect_count += 1
                uptime = asyncio.get_event_loop().time() - connected_at
                logger.warning(
                    "WS disconnected — reconnecting",
                    error=str(exc),
                    backoff_s=backoff,
                    uptime_s=round(uptime, 1),
                    total_reconnects=self._reconnect_count,
                )
                await asyncio.sleep(backoff)

                # If connection lasted >30s, it was a transient drop — reset backoff.
                # Otherwise escalate (rapid-fire failures like auth/size errors).
                backoff = _INITIAL_BACKOFF if uptime > 30 else min(backoff * 2, _MAX_BACKOFF)

                # Fire callback so the poller can backfill trades missed during the gap.
                if self._on_reconnect is not None:
                    try:
                        await self._on_reconnect()
                    except Exception as cb_exc:
                        logger.warning("on_reconnect callback failed", error=str(cb_exc))

    def stop(self) -> None:
        """Signal the listener to stop after the current iteration."""
        self._running = False

    @property
    def trade_count(self) -> int:
        return self._trade_count

    @property
    def book_event_count(self) -> int:
        """Individual price-change entries received (multiple per WS message)."""
        return self._book_event_count

    @property
    def book_message_count(self) -> int:
        """WS messages containing price_changes (one message may have many entries)."""
        return self._book_message_count

    @property
    def reconnect_count(self) -> int:
        """Total number of WS reconnections since startup."""
        return self._reconnect_count

    async def update_subscriptions(self, new_asset_ids: list[str]) -> None:
        """Subscribe to additional asset_ids on the live WS connection.

        Deduplicates against already-subscribed IDs.  When called between
        reconnects (``_ws`` is None), the new IDs are queued so the next
        ``_subscribe()`` call includes them automatically.
        """
        novel = [aid for aid in new_asset_ids if aid not in self._asset_ids]
        if not novel:
            return
        self._asset_ids.extend(novel)
        if self._ws is not None:
            payload = {
                "auth": {},
                "type": "subscribe",
                "markets": [],
                "assets_ids": novel,
            }
            try:
                await self._ws.send(json.dumps(payload))
                logger.info(
                    "WS subscription updated",
                    new_assets=len(novel),
                    total_assets=len(self._asset_ids),
                )
            except Exception as exc:
                logger.warning("WS subscription update failed", error=str(exc))
        else:
            logger.debug(
                "WS not connected — new asset_ids queued for next connect",
                queued=len(novel),
                total_assets=len(self._asset_ids),
            )

    async def set_subscriptions(self, new_asset_ids: list[str]) -> None:
        """Replace the subscription list with new_asset_ids (hot-tier refresh).

        Unlike ``update_subscriptions``, this *replaces* ``_asset_ids`` so that
        stale markets are dropped on the next reconnect.  On an active
        connection, only genuinely new IDs are sent as a subscribe message
        (Polymarket WS has no unsubscribe command, so already-subscribed
        channels stay active until reconnect).
        """
        novel = [aid for aid in new_asset_ids if aid not in set(self._asset_ids)]
        self._asset_ids = list(new_asset_ids)  # replace, not extend
        if not novel:
            logger.debug(
                "WS subscriptions replaced — no new assets",
                total_assets=len(self._asset_ids),
            )
            return
        if self._ws is not None:
            payload = {
                "auth": {},
                "type": "subscribe",
                "markets": [],
                "assets_ids": novel,
            }
            try:
                await self._ws.send(json.dumps(payload))
                logger.info(
                    "WS subscriptions replaced",
                    new_assets=len(novel),
                    total_assets=len(self._asset_ids),
                )
            except Exception as exc:
                logger.warning("WS subscription replace failed", error=str(exc))
        else:
            logger.debug(
                "WS not connected — subscription list replaced for next connect",
                new_assets=len(novel),
                total_assets=len(self._asset_ids),
            )

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
            max_size=10 * 1024 * 1024,  # 10 MB — Polymarket sends >1 MB with 100+ assets
        ) as ws:
            self._ws = ws
            try:
                await self._subscribe(ws)
                logger.info(
                    "WS connected and subscribed",
                    assets=len(self._asset_ids) or "none",
                )

                last_data = asyncio.get_event_loop().time()

                async def _watchdog() -> None:
                    """Close WS if no data received for 60s."""
                    while self._running:
                        await asyncio.sleep(15)
                        idle = asyncio.get_event_loop().time() - last_data
                        if idle > 60:
                            logger.warning("WS data stall — forcing reconnect", idle_s=round(idle))
                            await ws.close()
                            return

                watchdog = asyncio.create_task(_watchdog())
                try:
                    async for raw in ws:
                        if not self._running:
                            break
                        last_data = asyncio.get_event_loop().time()
                        await self._handle_message(raw)
                finally:
                    watchdog.cancel()
            finally:
                self._ws = None

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
        logger.info(
            "WS subscription sent",
            asset_count=len(self._asset_ids),
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
            await self._on_trade(trade)
            return

        # ── 2. Price changes (order book updates) ────────────────────
        if "price_changes" in msg:
            self._book_message_count += 1
            events = parse_price_changes(msg)
            for evt in events:
                self._book_event_count += 1
                if self._on_book_event is not None:
                    await self._on_book_event(evt)
            return

        # ── 3. Book snapshot (bids/asks) — log only ────────────────────
        if "bids" in msg or "asks" in msg:
            return

        # ── 4. Order placement/update — skip with logging ──────────────
        # Messages with fee_rate_bps but no event_type are order-level
        # notifications (placements/cancellations), not trade executions.
        # We log the first few so new Polymarket message types are visible.
        if "fee_rate_bps" in msg:
            self._fee_rate_skip_count = getattr(self, "_fee_rate_skip_count", 0) + 1
            if self._fee_rate_skip_count <= 5:
                logger.debug(
                    "Skipping order-level fee_rate_bps message",
                    keys=sorted(msg.keys()),
                    event_type=msg.get("event_type"),
                )
            elif self._fee_rate_skip_count % 1000 == 0:
                logger.info(
                    "fee_rate_bps messages skipped",
                    total=self._fee_rate_skip_count,
                )
            return

        # ── 5. Unknown ─────────────────────────────────────────────────
        self._unknown_count = getattr(self, "_unknown_count", 0) + 1
        if self._unknown_count <= 10:
            logger.warning("Unhandled WS message (sample)", keys=sorted(msg.keys())[:8])
        elif self._unknown_count % 500 == 0:
            logger.info("Unhandled WS messages", total_unknown=self._unknown_count)
