"""REST trade poller for Polymarket trade executions.

Periodically polls ``/trades?condition_id={cid}`` on the Polymarket data-api
for each tracked market, parses the flat trade objects into :class:`Trade`
objects, and forwards them to the batch writer via the same callback used by
the WS listener.

This complements the WebSocket listener: the ``/ws/market`` channel delivers
order-book events (price_changes, snapshots) but **not** trade executions.
The data-api ``/trades`` endpoint is the public, unauthenticated source of
actual trade executions with wallet addresses.

Two polling tiers:

* **Hot** — the top-N most active markets (from ``/sampling-markets``),
  polled every ``trade_poll_interval_seconds`` (default 30 s).
* **Cold** — a rotating window over *all* synced markets from the DB, polled
  ``trade_poll_cold_batch`` markets per cycle every
  ``trade_poll_cold_interval_seconds`` (default 60 s).  This ensures even
  low-activity markets are checked periodically, so a whale entering an
  obscure market isn't missed.
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

# Bounded LRU seen-set to avoid reprocessing recent trades
_SEEN_SET_MAX = 50_000


class _BoundedSet:
    """Order-preserving bounded set for dedup (evicts oldest on overflow)."""

    def __init__(self, maxlen: int = _SEEN_SET_MAX) -> None:
        self._data: OrderedDict[str, None] = OrderedDict()
        self._maxlen = maxlen

    def __contains__(self, key: str) -> bool:
        return key in self._data

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
    """Async REST poller for Polymarket trade executions.

    Supports two tiers:

    * **hot** — a small set of high-activity markets polled frequently.
    * **cold** — a rotating window through *all* synced markets, polled
      at a slower cadence so every market is eventually checked.

    Both tiers share a single dedup set so trades discovered by either
    tier are never forwarded twice.

    Usage::

        poller = TradePoller(
            on_trade=writer.add,
            condition_ids=["0xabc...", "0xdef..."],       # hot
            cold_condition_ids=["0x001...", "0x002..."],   # cold (all DB markets)
        )
        await poller.run()   # blocks until cancelled
    """

    def __init__(
        self,
        *,
        on_trade: TradeCallback,
        condition_ids: list[str] | None = None,
        cold_condition_ids: list[str] | None = None,
        cold_batch_size: int | None = None,
        cold_interval: int | None = None,
        poll_interval: int | None = None,
        base_url: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self._on_trade = on_trade
        self._condition_ids = list(condition_ids or [])
        self._cold_ids = list(cold_condition_ids or [])
        self._cold_batch = cold_batch_size or settings.trade_poll_cold_batch
        self._cold_interval = cold_interval or settings.trade_poll_cold_interval_seconds
        self._cold_offset = 0
        self._cold_poll_count = 0
        self._cold_trade_count = 0
        self._poll_interval = poll_interval or settings.trade_poll_interval_seconds
        self._base_url = base_url or settings.polymarket_data_api_url
        self._dry_run = dry_run
        self._trade_count = 0
        self._poll_count = 0
        self._running = False
        self._seen = _BoundedSet()

    # ── public API ──────────────────────────────────────────────────────

    @property
    def trade_count(self) -> int:
        return self._trade_count

    @property
    def poll_count(self) -> int:
        return self._poll_count

    @property
    def cold_poll_count(self) -> int:
        return self._cold_poll_count

    @property
    def cold_trade_count(self) -> int:
        return self._cold_trade_count

    def update_markets(self, condition_ids: list[str]) -> None:
        """Hot-swap the set of hot-tier tracked markets."""
        self._condition_ids = list(condition_ids)
        logger.info("Trade poller hot markets updated", count=len(self._condition_ids))

    def update_cold_markets(self, condition_ids: list[str]) -> None:
        """Replace the full cold-tier market list (e.g. after market sync)."""
        # Remove any that are already in the hot set to avoid double-polling
        hot_set = set(self._condition_ids)
        self._cold_ids = [c for c in condition_ids if c not in hot_set]
        self._cold_offset = 0
        logger.info("Trade poller cold markets updated", count=len(self._cold_ids))

    def stop(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Run hot + cold polling loops concurrently until cancelled."""
        self._running = True
        logger.info(
            "Trade poller starting",
            hot_markets=len(self._condition_ids),
            cold_markets=len(self._cold_ids),
            hot_interval_s=self._poll_interval,
            cold_interval_s=self._cold_interval,
            cold_batch=self._cold_batch,
        )

        tasks = [asyncio.create_task(self._run_hot(), name="poller_hot")]
        if self._cold_ids:
            tasks.append(asyncio.create_task(self._run_cold(), name="poller_cold"))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()

    # ── hot tier ────────────────────────────────────────────────────────

    async def _run_hot(self) -> None:
        """Poll hot-tier markets in a loop."""
        while self._running:
            try:
                await self._poll_batch(self._condition_ids, "hot")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Hot poll cycle failed", error=str(exc))
            await asyncio.sleep(self._poll_interval)

    # ── cold tier (rotating window) ─────────────────────────────────────

    async def _run_cold(self) -> None:
        """Poll a rotating slice of cold-tier markets in a loop."""
        while self._running:
            try:
                batch = self._next_cold_batch()
                if batch:
                    await self._poll_batch(batch, "cold")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Cold poll cycle failed", error=str(exc))
            await asyncio.sleep(self._cold_interval)

    def _next_cold_batch(self) -> list[str]:
        """Return the next slice of cold markets and advance the offset."""
        if not self._cold_ids:
            return []
        start = self._cold_offset
        end = start + self._cold_batch
        batch = self._cold_ids[start:end]
        # Wrap around if we've gone past the end
        if end >= len(self._cold_ids):
            self._cold_offset = 0
        else:
            self._cold_offset = end
        return batch

    # ── shared internals ────────────────────────────────────────────────

    async def _poll_batch(self, condition_ids: list[str], tier: str) -> None:
        """Poll a list of markets in parallel (bounded concurrency)."""
        if not condition_ids:
            return

        if tier == "hot":
            self._poll_count += 1
        else:
            self._cold_poll_count += 1

        sem = asyncio.Semaphore(10)

        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            async def _poll_one(cid: str) -> tuple[int, int]:
                async with sem:
                    return await self._poll_market(cid, client)

            results = await asyncio.gather(
                *[_poll_one(cid) for cid in condition_ids],
                return_exceptions=True,
            )

        new_trades = sum(r[0] for r in results if isinstance(r, tuple))
        total_fetched = sum(r[1] for r in results if isinstance(r, tuple))
        errors = sum(1 for r in results if isinstance(r, Exception))
        dedup = total_fetched - new_trades

        if tier == "cold":
            self._cold_trade_count += new_trades

        logger.info(
            "poll_cycle",
            tier=tier,
            new=new_trades,
            fetched=total_fetched,
            dedup=dedup,
            errors=errors if errors else None,
            total=self._trade_count,
            markets=len(condition_ids),
        )

    # kept for backward compatibility (tests)
    async def _poll_all(self) -> None:
        await self._poll_batch(self._condition_ids, "hot")

    async def _poll_market(
        self, condition_id: str, client: httpx.AsyncClient
    ) -> tuple[int, int]:
        """Fetch and process trades for a single market from the data-api.

        Returns (new_count, total_fetched) — total_fetched includes deduped trades.
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

            for raw_event in events:
                if not isinstance(raw_event, dict):
                    continue

                trade = parse_data_api_trade(raw_event)
                if trade is None:
                    continue

                total_fetched += 1

                # Dedup: skip if we've already seen this trade
                if trade.trade_id in self._seen:
                    continue

                self._seen.add(trade.trade_id)
                self._trade_count += 1
                new_count += 1
                await self._on_trade(trade)

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
