"""REST trade poller for Polymarket trade executions.

Periodically polls ``/trades?condition_id={cid}`` on the Polymarket data-api
for each market in the hot tier, parses the flat trade objects into
:class:`Trade` objects, and forwards them to the batch writer via the same
callback used by the WS listener.

Hot tier markets are selected by the LLM attractiveness priority formula and
refreshed every 30 minutes via ``_periodic_hot_market_refresh`` in
``__main__.py``.

Cold-tier trade polling has been removed.  Market discovery is handled
exclusively by the periodic ``sync_markets`` job which crawls
``/clob.polymarket.com/markets``.
"""

from __future__ import annotations

import asyncio
import ssl
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import structlog

from sentinel.config import settings
from sentinel.ingester.models import Trade, parse_data_api_trade

logger = structlog.get_logger()

# Type alias — same as the listener callback
TradeCallback = Callable[[Trade], Coroutine[Any, Any, None]]

# Lenient SSL context (matches listener.py)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# Bounded LRU seen-set to avoid reprocessing recent trades.
# The REST API returns the most recent N trades per market (not just new ones),
# so the set must hold IDs across many poll cycles without evicting.
# 500K holds ~5 full cycles (100 markets x 1000 trades x 5 cycles).
_SEEN_SET_MAX = 500_000


class _BoundedSet:
    """Order-preserving bounded set for dedup (evicts oldest on overflow)."""

    def __init__(self, maxlen: int = _SEEN_SET_MAX) -> None:
        self._data: OrderedDict[str, None] = OrderedDict()
        self._maxlen = maxlen

    def __contains__(self, key: str) -> bool:
        if key in self._data:
            self._data.move_to_end(key)
            return True
        return False

    def add(self, key: str) -> None:
        if key in self._data:
            self._data.move_to_end(key)
            return
        self._data[key] = None
        if len(self._data) > self._maxlen:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)


class TradePoller:
    """Async REST poller for Polymarket trade executions (hot tier only).

    Polls a curated set of high-attractiveness markets at ``poll_interval``
    seconds.  Market selection is handled externally by the priority formula
    and hot-swapped via ``update_markets()``.

    Usage::

        poller = TradePoller(
            on_trade=writer.add,
            condition_ids=["0xabc...", "0xdef..."],
        )
        await poller.run()   # blocks until cancelled
    """

    def __init__(
        self,
        *,
        on_trade: TradeCallback,
        condition_ids: list[str] | None = None,
        poll_interval: int | None = None,
        base_url: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self._on_trade = on_trade
        self._condition_ids = list(condition_ids or [])
        self._poll_interval = poll_interval or settings.trade_poll_interval_seconds
        self._base_url = base_url or settings.polymarket_data_api_url
        self._dry_run = dry_run
        self._trade_count = 0
        self._poll_count = 0
        self._running = False
        self._seen = _BoundedSet()
        self._market_max_ts: dict[str, float] = {}  # condition_id → max epoch seen

    # ── public API ──────────────────────────────────────────────────────

    @property
    def trade_count(self) -> int:
        return self._trade_count

    @property
    def poll_count(self) -> int:
        return self._poll_count

    def update_markets(self, condition_ids: list[str]) -> None:
        """Hot-swap the set of tracked markets."""
        self._condition_ids = list(condition_ids)
        logger.info("Trade poller markets updated", count=len(self._condition_ids))

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Run the polling loop until cancelled."""
        self._running = True
        logger.info(
            "Trade poller starting",
            markets=len(self._condition_ids),
            interval_s=self._poll_interval,
        )

        try:
            while self._running:
                try:
                    await self._poll_batch(self._condition_ids)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.error("Poll cycle failed", error=str(exc))
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            pass

    # ── internals ───────────────────────────────────────────────────────

    async def _poll_batch(self, condition_ids: list[str]) -> None:
        """Poll a list of markets in parallel (bounded concurrency)."""
        if not condition_ids:
            return

        self._poll_count += 1
        sem = asyncio.Semaphore(10)

        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            async def _poll_one(cid: str) -> tuple[int, int]:
                async with sem:
                    return await self._poll_market(cid, client)

            # Fire in chunks of 10 with a 200ms inter-chunk delay (~25 req/s).
            # With hot tier at 50 markets this stays well under rate limits.
            chunk_size = 10
            results: list[tuple[int, int] | BaseException] = []
            for i in range(0, len(condition_ids), chunk_size):
                chunk = condition_ids[i : i + chunk_size]
                chunk_results = await asyncio.gather(
                    *[_poll_one(cid) for cid in chunk],
                    return_exceptions=True,
                )
                results.extend(chunk_results)
                if i + chunk_size < len(condition_ids):
                    await asyncio.sleep(0.2)

        new_trades = sum(r[0] for r in results if isinstance(r, tuple))
        total_fetched = sum(r[1] for r in results if isinstance(r, tuple))
        errors = sum(1 for r in results if isinstance(r, Exception))
        dedup = total_fetched - new_trades

        logger.info(
            "poll_cycle",
            new=new_trades,
            fetched=total_fetched,
            dedup=dedup,
            errors=errors if errors else None,
            total=self._trade_count,
            markets=len(condition_ids),
        )

    # kept for backward compatibility (tests)
    async def _poll_all(self) -> None:
        await self._poll_batch(self._condition_ids)

    async def _poll_market(
        self, condition_id: str, client: httpx.AsyncClient
    ) -> tuple[int, int]:
        """Fetch and process trades for a single market.

        Returns (new_count, total_fetched).
        """
        url = f"{self._base_url}/trades"
        new_count = 0
        total_fetched = 0

        try:
            resp = await client.get(
                url, params={"condition_id": condition_id, "limit": settings.trade_poll_limit}
            )

            if resp.status_code == 404:
                return 0, 0

            resp.raise_for_status()
            events = resp.json()

            if not isinstance(events, list):
                logger.warning(
                    "Unexpected response from data-api /trades",
                    market=condition_id[:16],
                    type=type(events).__name__,
                )
                return 0, 0

            max_ts = self._market_max_ts.get(condition_id, 0.0)
            batch_max_ts = max_ts

            for raw_event in events:
                if not isinstance(raw_event, dict):
                    continue

                trade = parse_data_api_trade(raw_event)
                if trade is None:
                    continue

                total_fetched += 1
                trade_epoch = trade.timestamp.timestamp()

                # Skip trades at or before the last seen timestamp for this market.
                # Handles the sliding-window problem: high-volume markets rotate
                # their "newest 1000" each cycle, producing false "new" trades.
                if trade_epoch <= max_ts:
                    continue

                if trade.trade_id in self._seen:
                    continue

                batch_max_ts = max(batch_max_ts, trade_epoch)
                self._seen.add(trade.trade_id)
                self._trade_count += 1
                new_count += 1
                await self._on_trade(trade)

            if batch_max_ts > max_ts:
                self._market_max_ts[condition_id] = batch_max_ts

            if new_count > 0:
                logger.debug(
                    "market_polled",
                    market=condition_id[:16],
                    new=new_count,
                    fetched=total_fetched,
                    dedup=total_fetched - new_count,
                )

        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Trade poll request failed",
                market=condition_id[:16],
                status=exc.response.status_code,
            )
        except Exception as exc:
            logger.warning(
                "Trade poll request error",
                market=condition_id[:16],
                error=str(exc)[:120],
            )

        return new_count, total_fetched
