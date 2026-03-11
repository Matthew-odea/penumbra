"""Tests for backfill script and market sync logic."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from sentinel.db.init import init_schema
from sentinel.ingester.markets import _parse_end_date, upsert_markets
from sentinel.ingester.models import parse_rest_trade


# ── market sync tests ──────────────────────────────────────────────────────


class TestParseEndDate:
    def test_iso_with_z(self):
        dt = _parse_end_date("2026-06-01T00:00:00Z")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 6

    def test_none_input(self):
        assert _parse_end_date(None) is None

    def test_empty_string(self):
        assert _parse_end_date("") is None

    def test_invalid_format(self):
        assert _parse_end_date("not-a-date") is None


class TestUpsertMarkets:
    @pytest.fixture()
    def db_conn(self, tmp_path):
        conn = init_schema(tmp_path / "test.duckdb")
        yield conn
        conn.close()

    def _make_market(self, condition_id="0xabc", **overrides):
        base = {
            "condition_id": condition_id,
            "question": "Will X happen?",
            "market_slug": "will-x-happen",
            "tags": ["Biotech"],
            "end_date_iso": "2026-12-31T00:00:00Z",
            "volume": "1500000",
            "liquidity": "250000",
            "tokens": [
                {"token_id": "0xtok1", "outcome": "Yes", "price": 0.73},
                {"token_id": "0xtok2", "outcome": "No", "price": 0.27},
            ],
        }
        base.update(overrides)
        return base

    def test_basic_upsert(self, db_conn):
        markets = [self._make_market()]
        count = upsert_markets(db_conn, markets)
        assert count == 1
        row = db_conn.execute("SELECT market_id, question FROM markets").fetchone()
        assert row[0] == "0xabc"
        assert row[1] == "Will X happen?"

    def test_upsert_updates_existing(self, db_conn):
        m = self._make_market(question="Version 1")
        upsert_markets(db_conn, [m])
        m2 = self._make_market(question="Version 2")
        upsert_markets(db_conn, [m2])
        row = db_conn.execute("SELECT question FROM markets").fetchone()
        assert row[0] == "Version 2"
        total = db_conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        assert total == 1

    def test_multiple_markets(self, db_conn):
        markets = [
            self._make_market(condition_id="0x1"),
            self._make_market(condition_id="0x2"),
            self._make_market(condition_id="0x3"),
        ]
        count = upsert_markets(db_conn, markets)
        assert count == 3

    def test_empty_list(self, db_conn):
        assert upsert_markets(db_conn, []) == 0

    def test_tags_stored_as_category(self, db_conn):
        m = self._make_market(tags=["Politics", "USA"])
        upsert_markets(db_conn, [m])
        row = db_conn.execute("SELECT category FROM markets").fetchone()
        assert row[0] == "Politics,USA"


# ── backfill pagination tests ──────────────────────────────────────────────


class TestBackfillPagination:
    """Test that the backfill logic correctly handles pagination and cutoffs."""

    def _make_rest_trade(self, trade_id, timestamp_epoch):
        return {
            "id": trade_id,
            "market": "0xmarket",
            "asset_id": "0xtok",
            "side": "BUY",
            "size": "100.00",
            "price": "0.50",
            "timestamp": str(timestamp_epoch),
            "transaction_hash": "0xhash",
            "taker_address": "0xwallet",
        }

    def test_parse_rest_trade_from_backfill_data(self):
        raw = self._make_rest_trade("t-100", 1710000000)
        trade = parse_rest_trade(raw, market_id="0xoverride")
        assert trade is not None
        assert trade.trade_id == "t-100"
        assert trade.market_id == "0xoverride"
        assert trade.timestamp.year == 2024

    def test_parse_rest_trade_missing_required(self):
        raw = {"not_an_id": "x", "price": "0.5", "size": "100"}
        assert parse_rest_trade(raw) is None
