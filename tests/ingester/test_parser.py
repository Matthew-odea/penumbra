"""Tests for Trade, BookEvent models and WS/REST message parsers."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sentinel.ingester.models import (
    BookEvent,
    Trade,
    parse_data_api_trade,
    parse_live_activity_event,
    parse_price_changes,
    parse_rest_trade,
    parse_ws_trade,
)

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
        # Missing id is tolerated — a fallback trade_id is generated
        trade = parse_ws_trade(msg)
        assert trade is not None
        assert trade.trade_id.startswith("ws-")

    def test_returns_none_for_bad_price(self):
        msg = _ws_trade_msg(price="not-a-number")
        assert parse_ws_trade(msg) is None

    def test_optional_tx_hash(self):
        msg = _ws_trade_msg()
        msg["data"]["transaction_hash"] = None
        trade = parse_ws_trade(msg)
        assert trade is not None
        assert trade.tx_hash is None

    def test_flat_order_message_ignored(self):
        """Flat messages with fee_rate_bps are order updates, not trades."""
        msg = {
            "market": "0xabc",
            "asset_id": "0xtok",
            "price": "0.65",
            "size": "250.00",
            "fee_rate_bps": "20",
        }
        assert parse_ws_trade(msg) is None

    def test_non_trade_message_still_ignored(self):
        """Messages without trade-like keys return None."""
        msg = {"type": "heartbeat", "ts": 12345}
        assert parse_ws_trade(msg) is None


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
        assert len(tup) == 10
        assert tup[0] == "trade-001"  # trade_id
        assert isinstance(tup[5], float)  # price as float for DuckDB
        assert isinstance(tup[6], float)  # size_usd as float

    def test_as_dict(self):
        trade = parse_ws_trade(_ws_trade_msg())
        assert trade is not None
        d = trade.as_dict()
        assert d["trade_id"] == "trade-001"
        assert d["price"] == "0.73"  # String representation
        assert d["type"] == "trade"
        assert "timestamp" in d


# ── BookEvent / parse_price_changes tests ───────────────────────────────────


def _price_change_msg(
    *,
    market: str = "0xabc123",
    asset_id: str = "1154620873369832",
    price: str = "0.03",
    size: str = "41010",
    side: str = "BUY",
    hash_: str = "44baae6a60a0e4d65de84fd9738d45f0",
    best_bid: str = "0.027",
    best_ask: str = "0.029",
    num_changes: int = 1,
) -> dict:
    changes = []
    for i in range(num_changes):
        changes.append({
            "asset_id": f"{asset_id}_{i}" if num_changes > 1 else asset_id,
            "price": price,
            "size": size,
            "side": side,
            "hash": f"{hash_}_{i}" if num_changes > 1 else hash_,
            "best_bid": best_bid,
            "best_ask": best_ask,
        })
    return {"market": market, "price_changes": changes}


class TestParsePriceChanges:
    def test_basic_single_change(self):
        msg = _price_change_msg()
        events = parse_price_changes(msg)
        assert len(events) == 1
        evt = events[0]
        assert isinstance(evt, BookEvent)
        assert evt.market_id == "0xabc123"
        assert evt.asset_id == "1154620873369832"
        assert evt.side == "BUY"
        assert evt.price == Decimal("0.03")
        assert evt.size == Decimal("41010")
        assert evt.best_bid == Decimal("0.027")
        assert evt.best_ask == Decimal("0.029")
        assert evt.event_id == "44baae6a60a0e4d65de84fd9738d45f0"

    def test_multiple_changes(self):
        msg = _price_change_msg(num_changes=3)
        events = parse_price_changes(msg)
        assert len(events) == 3
        # Each should have a unique event_id
        ids = {e.event_id for e in events}
        assert len(ids) == 3

    def test_empty_price_changes(self):
        msg = {"market": "0xabc", "price_changes": []}
        events = parse_price_changes(msg)
        assert events == []

    def test_no_price_changes_key(self):
        msg = {"market": "0xabc", "bids": [{"price": "0.01", "size": "100"}]}
        events = parse_price_changes(msg)
        assert events == []

    def test_malformed_entry_skipped(self):
        msg = {
            "market": "0xabc",
            "price_changes": [
                {"asset_id": "tok1", "price": "0.5", "size": "100", "side": "BUY",
                 "hash": "h1", "best_bid": "0.4", "best_ask": "0.6"},
                {"bad": "entry"},  # missing required fields
                {"asset_id": "tok3", "price": "not-a-number", "size": "100",
                 "side": "SELL", "hash": "h3", "best_bid": "0.4", "best_ask": "0.6"},
            ],
        }
        events = parse_price_changes(msg)
        assert len(events) == 1  # only the first valid one
        assert events[0].event_id == "h1"

    def test_side_normalised_to_upper(self):
        msg = _price_change_msg(side="sell")
        events = parse_price_changes(msg)
        assert events[0].side == "SELL"

    def test_zero_size_is_cancellation(self):
        msg = _price_change_msg(size="0")
        events = parse_price_changes(msg)
        assert events[0].size == Decimal("0")

    def test_timestamp_is_utc(self):
        msg = _price_change_msg()
        events = parse_price_changes(msg)
        assert events[0].timestamp.tzinfo is not None

    def test_missing_best_bid_ask_defaults_to_zero(self):
        msg = {
            "market": "0xabc",
            "price_changes": [{
                "asset_id": "tok1",
                "price": "0.5",
                "size": "100",
                "side": "BUY",
                "hash": "h1",
            }],
        }
        events = parse_price_changes(msg)
        assert len(events) == 1
        assert events[0].best_bid == Decimal("0")
        assert events[0].best_ask == Decimal("0")


class TestBookEventModel:
    def test_frozen(self):
        msg = _price_change_msg()
        evt = parse_price_changes(msg)[0]
        with pytest.raises(AttributeError):
            evt.event_id = "new"  # type: ignore[misc]

    def test_as_dict(self):
        msg = _price_change_msg()
        evt = parse_price_changes(msg)[0]
        d = evt.as_dict()
        assert d["type"] == "book_event"
        assert d["market_id"] == "0xabc123"
        assert d["price"] == "0.03"
        assert d["size"] == "41010"
        assert "timestamp" in d


# ── Data-API trade parser tests ──────────────────────────────────────────────


def _data_api_trade(
    *,
    condition_id: str = "0xcondition123",
    asset: str = "0xtoken456",
    proxy_wallet: str = "0xwallet789",
    side: str = "BUY",
    size: float = 1500.00,
    price: float = 0.65,
    outcome: str = "Yes",
    outcome_index: int = 0,
    tx_hash: str = "0xtxhash",
    timestamp: int = 1710000000,
) -> dict:
    return {
        "proxyWallet": proxy_wallet,
        "side": side,
        "asset": asset,
        "conditionId": condition_id,
        "size": size,
        "price": price,
        "timestamp": timestamp,
        "transactionHash": tx_hash,
        "outcome": outcome,
        "outcomeIndex": outcome_index,
        "title": "Will X happen?",
        "slug": "will-x-happen",
        "name": "trader1",
        "pseudonym": "Anonymous",
        "icon": "https://example.com/icon.png",
        "eventSlug": "will-x-happen-event",
    }


class TestParseDataApiTrade:
    def test_basic(self):
        raw = _data_api_trade()
        trade = parse_data_api_trade(raw)
        assert trade is not None
        assert trade.market_id == "0xcondition123"
        assert trade.asset_id == "0xtoken456"
        assert trade.wallet == "0xwallet789"
        assert trade.side == "BUY"
        assert trade.price == Decimal("0.65")
        assert trade.size_usd == Decimal("1500")
        assert trade.tx_hash == "0xtxhash"
        assert trade.trade_id == "0xtxhash"  # tx_hash used as trade_id
        assert trade.timestamp.tzinfo is not None
        assert trade.source == "rest"

    def test_side_normalised_to_upper(self):
        trade = parse_data_api_trade(_data_api_trade(side="sell"))
        assert trade is not None
        assert trade.side == "SELL"

    def test_missing_condition_id_returns_none(self):
        raw = _data_api_trade()
        raw["conditionId"] = ""
        assert parse_data_api_trade(raw) is None

    def test_missing_wallet_returns_none(self):
        raw = _data_api_trade()
        raw["proxyWallet"] = ""
        assert parse_data_api_trade(raw) is None

    def test_bad_price_returns_none(self):
        raw = _data_api_trade(price="not-a-number")  # type: ignore
        assert parse_data_api_trade(raw) is None

    def test_missing_tx_hash_generates_fallback_id(self):
        raw = _data_api_trade()
        raw["transactionHash"] = None
        trade = parse_data_api_trade(raw)
        assert trade is not None
        assert trade.trade_id.startswith("da-")
        assert trade.tx_hash is None

    def test_missing_timestamp_uses_now(self):
        raw = _data_api_trade()
        del raw["timestamp"]
        trade = parse_data_api_trade(raw)
        assert trade is not None
        assert trade.timestamp.tzinfo is not None
        # Should be very recent
        from datetime import datetime, UTC
        delta = datetime.now(UTC) - trade.timestamp
        assert delta.total_seconds() < 5

    def test_empty_dict_returns_none(self):
        assert parse_data_api_trade({}) is None

    def test_completely_malformed_returns_none(self):
        assert parse_data_api_trade({"garbage": True}) is None

    def test_backward_compatible_alias(self):
        """parse_live_activity_event is an alias for parse_data_api_trade."""
        raw = _data_api_trade()
        trade = parse_live_activity_event(raw)
        assert trade is not None
        assert trade.market_id == "0xcondition123"
