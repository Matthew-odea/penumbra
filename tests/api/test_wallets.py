"""Tests for /api/wallets endpoints."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_get_wallet_profile(client):
    resp = await client.get("/api/wallets/0xAlpha")
    assert resp.status_code == 200
    body = resp.json()
    assert body["wallet"] == "0xAlpha"
    assert body["total_trades"] > 0
    assert isinstance(body["categories"], list)
    assert body["signal_count"] > 0


@pytest.mark.asyncio
async def test_get_wallet_profile_with_win_rate(client):
    """0xAlpha has 6 resolved trades so should have performance data."""
    resp = await client.get("/api/wallets/0xAlpha")
    body = resp.json()
    assert body["resolved_trades"] >= 5
    assert body["win_rate"] is not None


@pytest.mark.asyncio
async def test_get_wallet_unknown_returns_zeros(client):
    resp = await client.get("/api/wallets/0xUnknown")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_trades"] == 0
    assert body["win_rate"] is None


@pytest.mark.asyncio
async def test_get_wallet_trades(client):
    resp = await client.get("/api/wallets/0xAlpha/trades")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) > 0
    trade = data[0]
    assert "trade_id" in trade
    assert "market_question" in trade
    assert "timestamp" in trade


@pytest.mark.asyncio
async def test_get_wallet_trades_limit(client):
    resp = await client.get("/api/wallets/0xAlpha/trades", params={"limit": 2})
    data = resp.json()
    assert len(data) <= 2


@pytest.mark.asyncio
async def test_get_wallet_signals(client):
    resp = await client.get("/api/wallets/0xAlpha/signals")
    assert resp.status_code == 200
    data = resp.json()
    for sig in data:
        assert sig["wallet"] == "0xAlpha"
