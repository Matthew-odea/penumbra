"""Data models for the Polymarket ingester.

Two primary event types flow through the pipeline:

* **Trade** — an actual trade execution (when available via WS or REST).
* **BookEvent** — a price-change from the order-book WebSocket feed.
  These are the *primary* real-time data source; they show order placement /
  cancellation *before* execution, which is ideal for informed-flow detection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Union

# ── Trade ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Trade:
    """Normalised trade received from the Polymarket WebSocket or REST API."""

    trade_id: str
    market_id: str  # condition_id
    asset_id: str  # token_id
    wallet: str  # taker_address
    side: str  # BUY or SELL
    price: Decimal  # 0-1 probability
    size_usd: Decimal  # trade size in USDC
    timestamp: datetime
    tx_hash: str | None = None
    source: str = "ws"  # 'ws' (WebSocket) or 'rest' (poller)

    # ── helpers ──────────────────────────────────────────────────────────
    def as_db_tuple(self) -> tuple:
        """Return a tuple matching the DuckDB ``trades`` INSERT order."""
        return (
            self.trade_id,
            self.market_id,
            self.asset_id,
            self.wallet,
            self.side,
            float(self.price),
            float(self.size_usd),
            self.timestamp,
            self.tx_hash,
            self.source,
        )

    def as_dict(self) -> dict:
        """Serialise to a dict (useful for --dry-run JSON output)."""
        return {
            "type": "trade",
            "trade_id": self.trade_id,
            "market_id": self.market_id,
            "asset_id": self.asset_id,
            "wallet": self.wallet,
            "side": self.side,
            "price": str(self.price),
            "size_usd": str(self.size_usd),
            "timestamp": self.timestamp.isoformat(),
            "tx_hash": self.tx_hash,
            "source": self.source,
        }


# ── BookEvent ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BookEvent:
    """A price-change event from the Polymarket order-book WebSocket.

    Each ``price_changes`` array entry in a WS message becomes one BookEvent.
    ``size == 0`` means the level was cancelled; a non-zero ``size`` means a
    new or updated resting order at that price.
    """

    event_id: str  # order hash from the WS message
    market_id: str  # condition_id (hex)
    asset_id: str  # token_id
    side: str  # BUY or SELL
    price: Decimal  # order price level
    size: Decimal  # order size (USDC); 0 = level removed
    best_bid: Decimal  # current best bid after this change
    best_ask: Decimal  # current best ask after this change
    timestamp: datetime  # ingestion timestamp (WS doesn't provide one for price_changes)

    # ── helpers ──────────────────────────────────────────────────────────

    def as_dict(self) -> dict:
        """Serialise to a dict (useful for --dry-run JSON output)."""
        return {
            "type": "book_event",
            "event_id": self.event_id,
            "market_id": self.market_id,
            "asset_id": self.asset_id,
            "side": self.side,
            "price": str(self.price),
            "size": str(self.size),
            "best_bid": str(self.best_bid),
            "best_ask": str(self.best_ask),
            "timestamp": self.timestamp.isoformat(),
        }


# Type alias used by the scanner queue
IngesterEvent = Union[Trade, BookEvent]


# ── Parsers ──────────────────────────────────────────────────────────────────


def parse_ws_trade(msg: dict) -> Trade | None:
    """Parse a raw WebSocket trade message into a ``Trade``.

    Expected shape (``event_type == "trade"``):

    .. code-block:: json

        {
          "event_type": "trade",
          "data": {
            "id": "trade-uuid",
            "market": "condition_id_hex",
            "asset_id": "token_id",
            "side": "BUY",
            "size": "150.00",
            "price": "0.73",
            "timestamp": "1710000000",
            "transaction_hash": "0x...",
            "taker_address": "0x..."
          }
        }

    Returns ``None`` when the message is not a trade or is malformed.
    """
    if msg.get("event_type") != "trade":
        return None

    data = msg.get("data")
    if not data:
        return None

    try:
        ts_raw = data.get("timestamp")
        if ts_raw is not None:
            ts = datetime.fromtimestamp(int(ts_raw), tz=UTC)
        else:
            ts = datetime.now(UTC)

        # trade_id: prefer "id", fall back to "trade_id", then generate
        trade_id = str(data.get("id") or data.get("trade_id") or f"ws-{ts.timestamp():.0f}-{data.get('asset_id', '')[:8]}")

        return Trade(
            trade_id=trade_id,
            market_id=str(data["market"]),
            asset_id=str(data["asset_id"]),
            wallet=str(data.get("taker_address", data.get("owner", ""))),
            side=str(data.get("side", "BUY")).upper(),
            price=Decimal(str(data["price"])),
            size_usd=Decimal(str(data["size"])),
            timestamp=ts,
            tx_hash=data.get("transaction_hash"),
        )
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return None


def parse_price_changes(msg: dict) -> list[BookEvent]:
    """Parse a WS message containing ``price_changes`` into BookEvent objects.

    Expected shape::

        {
          "market": "0x...",        // condition_id (hex)
          "price_changes": [
            {
              "asset_id": "115462...",
              "price": "0.03",
              "size": "41010",
              "side": "BUY",
              "hash": "44baae6a...",
              "best_bid": "0.027",
              "best_ask": "0.029"
            },
            ...
          ]
        }

    Returns an empty list if the message has no ``price_changes`` key or if
    all entries are malformed.
    """
    market_id = str(msg.get("market", ""))
    changes = msg.get("price_changes")
    if not changes:
        return []

    now = datetime.now(tz=UTC)
    events: list[BookEvent] = []

    for c in changes:
        try:
            events.append(
                BookEvent(
                    event_id=str(c.get("hash", "")),
                    market_id=market_id,
                    asset_id=str(c["asset_id"]),
                    side=str(c.get("side", "UNKNOWN")).upper(),
                    price=Decimal(str(c["price"])),
                    size=Decimal(str(c["size"])),
                    best_bid=Decimal(str(c.get("best_bid", "0"))),
                    best_ask=Decimal(str(c.get("best_ask", "0"))),
                    timestamp=now,
                )
            )
        except (KeyError, ValueError, TypeError, InvalidOperation):
            continue

    return events


def parse_rest_trade(raw: dict, market_id: str = "") -> Trade | None:
    """Parse a trade dict returned by the REST ``/trades`` endpoint.

    The REST payload uses the same field names as the WS ``data`` sub-object,
    but is *not* wrapped in an ``event_type`` envelope.
    """
    try:
        ts_raw = raw.get("timestamp") or raw.get("matchTime") or raw.get("created_at", "0")
        ts = datetime.fromtimestamp(int(ts_raw), tz=UTC)

        return Trade(
            trade_id=str(raw["id"]),
            market_id=market_id or str(raw.get("market", "")),
            asset_id=str(raw.get("asset_id", "")),
            wallet=str(raw.get("taker_address", raw.get("owner", ""))),
            side=str(raw.get("side", "UNKNOWN")).upper(),
            price=Decimal(str(raw["price"])),
            size_usd=Decimal(str(raw["size"])),
            timestamp=ts,
            tx_hash=raw.get("transaction_hash", raw.get("transactionHash")),
        )
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return None


def parse_data_api_trade(raw: dict) -> Trade | None:
    """Parse a trade from the Polymarket ``data-api`` ``/trades`` endpoint.

    Expected shape (flat JSON object)::

        {
          "proxyWallet": "0x...",
          "side": "BUY",
          "asset": "token_id_string",
          "conditionId": "0x...",
          "size": 5.43,
          "price": 0.92,
          "timestamp": 1710000000,
          "transactionHash": "0x...",
          "outcome": "Yes",
          "outcomeIndex": 0,
          "title": "...",
          "slug": "...",
          "name": "trader_name",
          "pseudonym": "..."
        }

    Returns ``None`` when the input is malformed or missing required fields.
    """
    try:
        tx_hash = raw.get("transactionHash")
        condition_id = str(raw.get("conditionId", ""))
        wallet = str(raw.get("proxyWallet", ""))

        if not condition_id or not wallet:
            return None

        ts_raw = raw.get("timestamp")
        if ts_raw is not None:
            ts = datetime.fromtimestamp(int(ts_raw), tz=UTC)
        else:
            ts = datetime.now(UTC)

        asset_id = str(raw.get("asset", ""))
        trade_id = str(tx_hash) if tx_hash else f"da-{ts.timestamp():.0f}-{asset_id[:8]}"

        return Trade(
            trade_id=trade_id,
            market_id=condition_id,
            asset_id=asset_id,
            wallet=wallet,
            side=str(raw.get("side", "BUY")).upper(),
            price=Decimal(str(raw["price"])),
            size_usd=Decimal(str(raw["size"])),
            timestamp=ts,
            tx_hash=str(tx_hash) if tx_hash else None,
            source="rest",
        )
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return None


# Backward-compatible alias
parse_live_activity_event = parse_data_api_trade
