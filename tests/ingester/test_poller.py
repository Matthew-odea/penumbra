"""Tests for the REST trade poller."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sentinel.ingester.models import Trade
from sentinel.ingester.poller import TradePoller, _BoundedSet


# ── _BoundedSet tests ───────────────────────────────────────────────────────


class TestBoundedSet:
    def test_add_and_contains(self):
        s = _BoundedSet(maxlen=5)
        s.add("a")
        assert "a" in s
        assert "b" not in s

    def test_evicts_oldest_on_overflow(self):
        s = _BoundedSet(maxlen=3)
        s.add("a")
        s.add("b")
        s.add("c")
        s.add("d")  # evicts "a"
        assert "a" not in s
        assert "b" in s
        assert "d" in s
        assert len(s) == 3

    def test_no_duplicates(self):
        s = _BoundedSet(maxlen=5)
        s.add("x")
        s.add("x")
        s.add("x")
        assert len(s) == 1

    def test_re_add_moves_to_end(self):
        s = _BoundedSet(maxlen=3)
        s.add("a")
        s.add("b")
        s.add("a")  # move "a" to end → order: b, a
        s.add("c")  # order: b, a, c (size==maxlen, no eviction yet)
        s.add("d")  # evicts "b" (oldest), not "a"
        assert "a" in s
        assert "b" not in s
        assert "c" in s
        assert "d" in s


# ── Fixtures ────────────────────────────────────────────────────────────────


def _live_event(
    *,
    condition_id: str = "0xcond1",
    asset: str = "0xasset1",
    proxy_wallet: str = "0xwallet1",
    tx_hash: str = "0xtx1",
    price: float = 0.60,
    size: float = 200.00,
    timestamp: int = 1710000000,
) -> dict:
    return {
        "proxyWallet": proxy_wallet,
        "side": "BUY",
        "asset": asset,
        "conditionId": condition_id,
        "size": size,
        "price": price,
        "timestamp": timestamp,
        "transactionHash": tx_hash,
        "outcome": "Yes",
        "outcomeIndex": 0,
        "title": "Test?",
        "slug": "test",
        "name": "user1",
        "pseudonym": "Anonymous",
    }


# ── TradePoller tests ───────────────────────────────────────────────────────


class TestTradePoller:
    @pytest.fixture
    def callback(self):
        return AsyncMock()

    def test_init_defaults(self, callback):
        poller = TradePoller(on_trade=callback)
        assert poller.trade_count == 0
        assert poller.poll_count == 0

    def test_update_markets(self, callback):
        poller = TradePoller(on_trade=callback, condition_ids=["0xa"])
        assert poller._condition_ids == ["0xa"]
        poller.update_markets(["0xb", "0xc"])
        assert poller._condition_ids == ["0xb", "0xc"]

    def test_stop(self, callback):
        poller = TradePoller(on_trade=callback)
        poller._running = True
        poller.stop()
        assert not poller._running

    @pytest.mark.asyncio
    async def test_poll_market_success(self, callback):
        events = [_live_event(tx_hash="0xtx1"), _live_event(tx_hash="0xtx2")]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = events

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        poller = TradePoller(on_trade=callback, condition_ids=["0xcond1"])
        count = await poller._poll_market("0xcond1", mock_client)

        assert count == 2
        assert callback.call_count == 2
        assert poller.trade_count == 2

    @pytest.mark.asyncio
    async def test_poll_market_deduplicates(self, callback):
        """Same tx_hash should not trigger callback twice."""
        events = [_live_event(tx_hash="0xsame"), _live_event(tx_hash="0xsame")]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = events

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        poller = TradePoller(on_trade=callback, condition_ids=["0xcond1"])
        count = await poller._poll_market("0xcond1", mock_client)

        assert count == 1
        assert callback.call_count == 1

    @pytest.mark.asyncio
    async def test_poll_market_dedup_across_calls(self, callback):
        """Trades seen in a previous poll cycle should still be deduped."""
        events = [_live_event(tx_hash="0xpersist")]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = events

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        poller = TradePoller(on_trade=callback, condition_ids=["0xcond1"])
        count1 = await poller._poll_market("0xcond1", mock_client)
        count2 = await poller._poll_market("0xcond1", mock_client)  # same events

        assert count1 == 1
        assert count2 == 0
        assert callback.call_count == 1

    @pytest.mark.asyncio
    async def test_poll_market_404_returns_zero(self, callback):
        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        poller = TradePoller(on_trade=callback, condition_ids=["0xnone"])
        count = await poller._poll_market("0xnone", mock_client)

        assert count == 0
        assert callback.call_count == 0

    @pytest.mark.asyncio
    async def test_poll_market_non_list_response(self, callback):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"error": "not found"}

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        poller = TradePoller(on_trade=callback, condition_ids=["0xcond1"])
        count = await poller._poll_market("0xcond1", mock_client)

        assert count == 0

    @pytest.mark.asyncio
    async def test_poll_market_malformed_events_skipped(self, callback):
        events = [
            _live_event(tx_hash="0xgood"),
            {"garbage": True},
            {"market": {}, "user": {}, "price": "bad"},
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = events

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        poller = TradePoller(on_trade=callback, condition_ids=["0xcond1"])
        count = await poller._poll_market("0xcond1", mock_client)

        assert count == 1  # only the good event
        assert callback.call_count == 1

    @pytest.mark.asyncio
    async def test_poll_all_no_markets(self, callback):
        poller = TradePoller(on_trade=callback, condition_ids=[])
        await poller._poll_all()
        assert poller.poll_count == 0

    @pytest.mark.asyncio
    async def test_poll_all_increments_count(self, callback):
        with patch.object(TradePoller, "_poll_market", return_value=0) as mock_poll:
            poller = TradePoller(
                on_trade=callback,
                condition_ids=["0xa", "0xb"],
            )
            await poller._poll_all()

        assert poller.poll_count == 1
        assert mock_poll.call_count == 2


class TestColdTier:
    """Tests for the cold-tier rotating-window logic."""

    @pytest.fixture
    def callback(self):
        return AsyncMock()

    def test_next_cold_batch_basic(self, callback):
        poller = TradePoller(
            on_trade=callback,
            cold_condition_ids=["a", "b", "c", "d", "e"],
            cold_batch_size=2,
        )
        assert poller._next_cold_batch() == ["a", "b"]
        assert poller._next_cold_batch() == ["c", "d"]
        # Last batch is smaller than batch_size → wraps offset to 0
        assert poller._next_cold_batch() == ["e"]
        # Starts over from the beginning
        assert poller._next_cold_batch() == ["a", "b"]

    def test_next_cold_batch_empty(self, callback):
        poller = TradePoller(on_trade=callback, cold_condition_ids=[])
        assert poller._next_cold_batch() == []

    def test_update_cold_markets_excludes_hot(self, callback):
        poller = TradePoller(
            on_trade=callback,
            condition_ids=["hot1", "hot2"],
        )
        poller.update_cold_markets(["hot1", "cold1", "cold2", "hot2", "cold3"])
        assert "hot1" not in poller._cold_ids
        assert "hot2" not in poller._cold_ids
        assert poller._cold_ids == ["cold1", "cold2", "cold3"]

    def test_cold_properties(self, callback):
        poller = TradePoller(on_trade=callback)
        assert poller.cold_poll_count == 0
        assert poller.cold_trade_count == 0

    @pytest.mark.asyncio
    async def test_poll_batch_cold_increments_cold_count(self, callback):
        with patch.object(TradePoller, "_poll_market", return_value=2):
            poller = TradePoller(
                on_trade=callback,
                cold_condition_ids=["c1", "c2"],
                cold_batch_size=2,
            )
            await poller._poll_batch(["c1", "c2"], "cold")

        assert poller.cold_poll_count == 1
        assert poller.cold_trade_count == 4  # 2 markets × 2 trades each

    @pytest.mark.asyncio
    async def test_poll_batch_hot_does_not_touch_cold_stats(self, callback):
        with patch.object(TradePoller, "_poll_market", return_value=1):
            poller = TradePoller(
                on_trade=callback,
                condition_ids=["h1"],
            )
            await poller._poll_batch(["h1"], "hot")

        assert poller.poll_count == 1
        assert poller.cold_poll_count == 0
        assert poller.cold_trade_count == 0


class TestGetAllConditionIds:
    """Test get_all_condition_ids helper."""

    def test_returns_sorted_ids(self):
        from sentinel.ingester.markets import get_all_condition_ids

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("0xaaa",), ("0xbbb",), ("0xccc",),
        ]

        result = get_all_condition_ids(mock_conn)
        assert result == ["0xaaa", "0xbbb", "0xccc"]
        mock_conn.execute.assert_called_once()

    def test_returns_empty_for_no_markets(self):
        from sentinel.ingester.markets import get_all_condition_ids

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        assert get_all_condition_ids(mock_conn) == []
    """Test that fetch_active_markets returns condition_ids + asset_ids."""

    @pytest.mark.asyncio
    async def test_fetch_active_markets_structure(self):
        from sentinel.ingester.markets import fetch_active_markets

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {
                    "condition_id": "0xcond1",
                    "tokens": [
                        {"token_id": "tok1a"},
                        {"token_id": "tok1b"},
                    ],
                },
                {
                    "condition_id": "0xcond2",
                    "tokens": [
                        {"token_id": "tok2a"},
                    ],
                },
            ],
        }

        with patch("sentinel.ingester.markets.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            result = await fetch_active_markets(base_url="http://test")

        assert len(result) == 2
        assert result[0] == ("0xcond1", ["tok1a", "tok1b"])
        assert result[1] == ("0xcond2", ["tok2a"])

    @pytest.mark.asyncio
    async def test_fetch_active_markets_skips_empty_condition_id(self):
        from sentinel.ingester.markets import fetch_active_markets

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = [
            {"condition_id": "", "tokens": [{"token_id": "tok1"}]},
            {"condition_id": "0xvalid", "tokens": [{"token_id": "tok2"}]},
        ]

        with patch("sentinel.ingester.markets.httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value = mock_instance

            result = await fetch_active_markets(base_url="http://test")

        assert len(result) == 1
        assert result[0][0] == "0xvalid"
