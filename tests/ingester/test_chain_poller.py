"""Tests for the on-chain trade poller (Polygon OrdersMatched events)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from sentinel.ingester.chain_poller import ChainPoller, build_token_map

# ── Fixtures ────────────────────────────────────────────────────────────────

# Real OrdersMatched log from Polygon (structure matches Alchemy eth_getLogs)
_SAMPLE_BUY_LOG = {
    "address": "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    "topics": [
        # OrdersMatched topic
        "0x63bf4d16b7fa898ef4c4b2b6d90fd201e9c56313b65638af6088d149d2ce956c",
        # takerOrderHash (indexed bytes32)
        "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        # takerOrderMaker (indexed address, zero-padded to 32 bytes)
        "0x000000000000000000000000abcdef1234567890abcdef1234567890abcdef12",
    ],
    # data: makerAssetId=12345 (token), takerAssetId=0 (USDC),
    #        makerAmountFilled=1000000 (1 token), takerAmountFilled=730000 (0.73 USDC)
    # Taker pays 0.73 USDC for 1 token → BUY at price 0.73
    "data": (
        "0x"
        "0000000000000000000000000000000000000000000000000000000000003039"  # makerAssetId = 12345
        "0000000000000000000000000000000000000000000000000000000000000000"  # takerAssetId = 0 (USDC)
        "00000000000000000000000000000000000000000000000000000000000f4240"  # makerAmountFilled = 1000000 (0xf4240)
        "00000000000000000000000000000000000000000000000000000000000b2390"  # takerAmountFilled = 730000 (0xb2390)
    ),
    "blockNumber": "0x5135500",
    "transactionHash": "0xdeadbeef0000000000000000000000000000000000000000000000000000cafe",
    "logIndex": "0x3",
}

_SAMPLE_SELL_LOG = {
    "address": "0xc5d563a36ae78145c45a50134d48a1215220f80a",
    "topics": [
        "0x63bf4d16b7fa898ef4c4b2b6d90fd201e9c56313b65638af6088d149d2ce956c",
        "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "0x000000000000000000000000fedcba0987654321fedcba0987654321fedcba09",
    ],
    # data: makerAssetId=0 (USDC), takerAssetId=67890 (token),
    #        makerAmountFilled=500000 (0.50 USDC), takerAmountFilled=2000000 (2 tokens)
    # Taker pays 2 tokens, receives 0.50 USDC → SELL at price 0.25
    "data": (
        "0x"
        "0000000000000000000000000000000000000000000000000000000000000000"  # makerAssetId = 0 (USDC)
        "0000000000000000000000000000000000000000000000000000000000010932"  # takerAssetId = 67890
        "000000000000000000000000000000000000000000000000000000000007a120"  # makerAmountFilled = 500000
        "00000000000000000000000000000000000000000000000000000000001e8480"  # takerAmountFilled = 2000000
    ),
    "blockNumber": "0x5135501",
    "transactionHash": "0xfeed000000000000000000000000000000000000000000000000000000001234",
    "logIndex": "0x7",
}

# Token-for-token swap (both assets non-zero) — should be skipped
_SAMPLE_SWAP_LOG = {
    "address": "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    "topics": [
        "0x63bf4d16b7fa898ef4c4b2b6d90fd201e9c56313b65638af6088d149d2ce956c",
        "0xcccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        "0x0000000000000000000000001111111111111111111111111111111111111111",
    ],
    "data": (
        "0x"
        "0000000000000000000000000000000000000000000000000000000000003039"  # makerAssetId = 12345 (token)
        "0000000000000000000000000000000000000000000000000000000000010932"  # takerAssetId = 67890 (token)
        "00000000000000000000000000000000000000000000000000000000000f4240"
        "00000000000000000000000000000000000000000000000000000000000f4240"
    ),
    "blockNumber": "0x5135502",
    "transactionHash": "0xaaaa000000000000000000000000000000000000000000000000000000005678",
    "logIndex": "0x1",
}

_TOKEN_MAP = {
    "12345": "condition-abc",
    "67890": "condition-def",
}


# ── Decode tests ────────────────────────────────────────────────────────────


class TestDecodeLog:
    def _make_poller(self, token_map: dict[str, str] | None = None) -> ChainPoller:
        return ChainPoller(
            on_trade=AsyncMock(),
            token_map=_TOKEN_MAP if token_map is None else token_map,
            rpc_url="https://fake-rpc.example.com",
        )

    def test_decode_buy(self) -> None:
        poller = self._make_poller()
        trade = poller._decode_log(_SAMPLE_BUY_LOG)

        assert trade is not None
        assert trade.side == "BUY"
        assert trade.wallet == "0xabcdef1234567890abcdef1234567890abcdef12"
        assert trade.market_id == "condition-abc"
        assert trade.asset_id == "12345"
        assert trade.source == "chain"
        assert trade.tx_hash == _SAMPLE_BUY_LOG["transactionHash"]
        # 730000 / 1e6 = 0.73 USDC
        assert trade.size_usd == Decimal("0.73")
        # price = 0.73 / 1.0 = 0.73
        assert trade.price == Decimal("0.73")
        assert trade.trade_id.startswith("chain-")

    def test_decode_sell(self) -> None:
        poller = self._make_poller()
        trade = poller._decode_log(_SAMPLE_SELL_LOG)

        assert trade is not None
        assert trade.side == "SELL"
        assert trade.wallet == "0xfedcba0987654321fedcba0987654321fedcba09"
        assert trade.market_id == "condition-def"
        assert trade.asset_id == "67890"
        # 500000 / 1e6 = 0.50 USDC
        assert trade.size_usd == Decimal("0.5")
        # price = 0.50 / 2.0 = 0.25
        assert trade.price == Decimal("0.25")

    def test_skip_token_swap(self) -> None:
        """Token-for-token swaps (both assets non-zero) should return None."""
        poller = self._make_poller()
        trade = poller._decode_log(_SAMPLE_SWAP_LOG)
        assert trade is None

    def test_skip_unknown_token(self) -> None:
        """Trades for tokens not in the map should return None."""
        poller = self._make_poller(token_map={})  # empty map
        trade = poller._decode_log(_SAMPLE_BUY_LOG)
        assert trade is None

    def test_skip_exchange_as_taker(self) -> None:
        """When taker is the exchange contract itself, skip the trade."""
        log = {**_SAMPLE_BUY_LOG, "topics": [
            _SAMPLE_BUY_LOG["topics"][0],
            _SAMPLE_BUY_LOG["topics"][1],
            # taker = CTF Exchange address
            "0x0000000000000000000000004bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
        ]}
        poller = self._make_poller()
        trade = poller._decode_log(log)
        assert trade is None

    def test_trade_id_format(self) -> None:
        """trade_id should be chain-{txhash}-{logIndex}."""
        poller = self._make_poller()
        trade = poller._decode_log(_SAMPLE_BUY_LOG)
        assert trade is not None
        expected = f"chain-{_SAMPLE_BUY_LOG['transactionHash']}-3"
        assert trade.trade_id == expected

    def test_malformed_log_returns_none(self) -> None:
        """Malformed logs should not raise, just return None."""
        poller = self._make_poller()
        assert poller._decode_log({}) is None
        assert poller._decode_log({"topics": [], "data": "0x"}) is None


# ── Token map builder tests ─────────────────────────────────────────────────


class TestBuildTokenMap:
    def test_build_from_db(self) -> None:
        """build_token_map should parse comma-separated token_ids."""

        class FakeConn:
            def execute(self, sql: str, *args: object) -> FakeConn:
                return self

            def fetchall(self) -> list[tuple[str, str]]:
                return [
                    ("cond-1", "111,222"),
                    ("cond-2", "333,444"),
                    ("cond-3", "555"),
                ]

        token_map = build_token_map(FakeConn(), signal_eligible_only=False)
        assert token_map == {
            "111": "cond-1",
            "222": "cond-1",
            "333": "cond-2",
            "444": "cond-2",
            "555": "cond-3",
        }

    def test_empty_token_ids_skipped(self) -> None:
        """Rows with empty token_ids should not produce map entries."""

        class FakeConn:
            def execute(self, sql: str, *args: object) -> FakeConn:
                return self

            def fetchall(self) -> list[tuple[str, str]]:
                return [("cond-1", ",,,")]

        token_map = build_token_map(FakeConn(), signal_eligible_only=False)
        assert token_map == {}


# ── Poll loop integration test ──────────────────────────────────────────────


class TestPollLoop:
    @pytest.mark.asyncio
    async def test_poll_dispatches_trades(self) -> None:
        """ChainPoller should decode logs and call on_trade for matched tokens."""
        on_trade = AsyncMock()
        poller = ChainPoller(
            on_trade=on_trade,
            token_map=_TOKEN_MAP,
            rpc_url="https://fake-rpc.example.com",
        )

        # Mock httpx.AsyncClient to return block number and logs
        mock_responses = [
            # First call: eth_blockNumber → block 100
            {"jsonrpc": "2.0", "id": 1, "result": hex(100)},
            # Second call (after _last_block is set): eth_blockNumber → block 105
            {"jsonrpc": "2.0", "id": 1, "result": hex(105)},
            # Third call: eth_getLogs → 1 BUY log
            {"jsonrpc": "2.0", "id": 2, "result": [_SAMPLE_BUY_LOG]},
        ]
        call_count = 0

        class FakeResponse:
            def __init__(self, data: dict) -> None:
                self._data = data

            def json(self) -> dict:
                return self._data

            def raise_for_status(self) -> None:
                pass

        async def mock_post(url: str, json: dict, **kwargs: object) -> FakeResponse:
            nonlocal call_count
            resp = FakeResponse(mock_responses[call_count])
            call_count += 1
            return resp

        with patch("sentinel.ingester.chain_poller.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = mock_post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # First poll: initializes _last_block
            await poller._poll_once()
            assert poller._last_block == 100
            assert on_trade.call_count == 0

            # Second poll: fetches logs and dispatches
            await poller._poll_once()
            assert on_trade.call_count == 1
            trade = on_trade.call_args[0][0]
            assert trade.side == "BUY"
            assert trade.wallet == "0xabcdef1234567890abcdef1234567890abcdef12"
            assert trade.source == "chain"
