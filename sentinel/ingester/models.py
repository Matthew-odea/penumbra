"""Data models for the Polymarket ingester."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation


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
        )

    def as_dict(self) -> dict:
        """Serialise to a dict (useful for --dry-run JSON output)."""
        return {
            "trade_id": self.trade_id,
            "market_id": self.market_id,
            "asset_id": self.asset_id,
            "wallet": self.wallet,
            "side": self.side,
            "price": str(self.price),
            "size_usd": str(self.size_usd),
            "timestamp": self.timestamp.isoformat(),
            "tx_hash": self.tx_hash,
        }


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
        ts_raw = data["timestamp"]
        # Polymarket sends epoch seconds as a string
        ts = datetime.fromtimestamp(int(ts_raw), tz=UTC)

        return Trade(
            trade_id=str(data["id"]),
            market_id=str(data["market"]),
            asset_id=str(data["asset_id"]),
            wallet=str(data.get("taker_address", "")),
            side=str(data["side"]).upper(),
            price=Decimal(str(data["price"])),
            size_usd=Decimal(str(data["size"])),
            timestamp=ts,
            tx_hash=data.get("transaction_hash"),
        )
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return None


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
