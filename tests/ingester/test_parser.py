"""Tests for Trade model and WS/REST message parsers."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentinel.ingester.models import Trade, parse_rest_trade, parse_ws_trade

# ── Fixtures ────────────────────────────────────────────────────────────────


def _ws_trade_msg(
    *,
    trade_id: str = "trade-001",
    market: str = "0xabc",
    asset_id: str = "0xtok",
    side: str = "BUY",
    size: str = "500.00",
    price: str = "0.73",
    timestamp: str = "1710000000",
    tx_hash: str = "0xdeadbeef",
    taker_address: str = "0xwallet",
) -> dict:
    return {
        "event_type": "trade",
        "data": {
            "id": trade_id,
            "market": market,
            "asset_id": asset_id,
            "side": side,
            "size": size,
            "price": price,
            "timestamp": timestamp,
            "transaction_hash": tx_hash,
            "taker_address": taker_address,
        },
    }


def _rest_trade(
    *,
    trade_id: str = "trade-002",
    market: str = "0xabc",
    asset_id: str = "0xtok",
    side: str = "SELL",
    size: str = "1000.00",
    price: str = "0.45",
    timestamp: str = "1710000000",
    tx_hash: str = "0xcafe",
    taker_address: str = "0xwallet2",
) -> dict:
    return {
        "id": trade_id,
        "market": market,
        "asset_id": asset_id,
        "side": side,
        "size": size,
        "price": price,
        "timestamp": timestamp,
        "transaction_hash": tx_hash,
        "taker_address": taker_address,
    }


# ── Happy-path tests ───────────────────────────────────────────────────────


class TestParseWsTrade:
    def test_basic(self):
        msg = _ws_trade_msg()
        trade = parse_ws_trade(msg)
        assert trade is not None
        assert trade.trade_id == "trade-001"
        assert trade.market_id == "0xabc"
        assert trade.asset_id == "0xtok"
        assert trade.wallet == "0xwallet"
        assert trade.side == "BUY"
        assert trade.price == Decimal("0.73")
        assert trade.size_usd == Decimal("500.00")
        assert trade.tx_hash == "0xdeadbeef"
        assert trade.timestamp.tzinfo is not None

    def test_timestamp_is_utc(self):
        trade = parse_ws_trade(_ws_trade_msg(timestamp="1710000000"))
        assert trade is not None
        assert trade.timestamp.tzinfo == UTC

    def test_side_normalised_to_upper(self):
        trade = parse_ws_trade(_ws_trade_msg(side="buy"))
        assert trade is not None
        assert trade.side == "BUY"

    def test_returns_none_for_non_trade_event(self):
        msg = {"event_type": "book", "data": {}}
        assert parse_ws_trade(msg) is None

    def test_returns_none_for_missing_data(self):
        msg = {"event_type": "trade"}
        assert parse_ws_trade(msg) is None

    def test_returns_none_for_missing_id(self):
        msg = _ws_trade_msg()
        del msg["data"]["id"]
        assert parse_ws_trade(msg) is None

    def test_returns_none_for_bad_price(self):
        msg = _ws_trade_msg(price="not-a-number")
        assert parse_ws_trade(msg) is None

    def test_optional_tx_hash(self):
        msg = _ws_trade_msg()
        msg["data"]["transaction_hash"] = None
        trade = parse_ws_trade(msg)
        assert trade is not None
        assert trade.tx_hash is None


class TestParseRestTrade:
    def test_basic(self):
        raw = _rest_trade()
        trade = parse_rest_trade(raw, market_id="0xoverride")
        assert trade is not None
        assert trade.trade_id == "trade-002"
        assert trade.market_id == "0xoverride"
        assert trade.side == "SELL"
        assert trade.price == Decimal("0.45")
        assert trade.size_usd == Decimal("1000.00")

    def test_uses_raw_market_if_no_override(self):
        raw = _rest_trade(market="0xfromapi")
        trade = parse_rest_trade(raw)
        assert trade is not None
        assert trade.market_id == "0xfromapi"

    def test_returns_none_for_missing_id(self):
        raw = _rest_trade()
        del raw["id"]
        assert parse_rest_trade(raw) is None


# ── Trade dataclass tests ──────────────────────────────────────────────────


class TestTradeModel:
    def test_frozen(self):
        trade = parse_ws_trade(_ws_trade_msg())
        assert trade is not None
        with pytest.raises(AttributeError):
            trade.trade_id = "new"  # type: ignore[misc]

    def test_as_db_tuple(self):
        trade = parse_ws_trade(_ws_trade_msg())
        assert trade is not None
        tup = trade.as_db_tuple()
        assert len(tup) == 9
        assert tup[0] == "trade-001"  # trade_id
        assert isinstance(tup[5], float)  # price as float for DuckDB
        assert isinstance(tup[6], float)  # size_usd as float

    def test_as_dict(self):
        trade = parse_ws_trade(_ws_trade_msg())
        assert trade is not None
        d = trade.as_dict()
        assert d["trade_id"] == "trade-001"
        assert d["price"] == "0.73"  # String representation
        assert "timestamp" in d
