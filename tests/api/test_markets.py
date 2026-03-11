"""Tests for /api/markets endpoints."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_list_markets(client):
    resp = await client.get("/api/markets")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    # Only active markets by default: mkt-001 and mkt-002
    assert len(data) >= 2
    for m in data:
        assert m["active"] is True


@pytest.mark.asyncio
async def test_list_markets_all_includes_inactive(client):
    resp = await client.get("/api/markets", params={"active_only": False})
    data = resp.json()
    ids = {m["market_id"] for m in data}
    assert "mkt-003" in ids  # resolved / inactive


@pytest.mark.asyncio
async def test_list_markets_has_signal_count(client):
    resp = await client.get("/api/markets")
    data = resp.json()
    for m in data:
        assert "signal_count" in m
        assert isinstance(m["signal_count"], int)


@pytest.mark.asyncio
async def test_get_market_detail(client):
    resp = await client.get("/api/markets/mkt-001")
    assert resp.status_code == 200
    m = resp.json()
    assert m["market_id"] == "mkt-001"
    assert m["question"] == "Will Bitcoin exceed $100k by June?"
    assert m["category"] == "Crypto"
    assert isinstance(m["volume_usd"], float)


@pytest.mark.asyncio
async def test_get_market_404(client):
    resp = await client.get("/api/markets/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_market_volume(client):
    resp = await client.get("/api/markets/mkt-001/volume")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    if data:
        assert "hour" in data[0]
        assert "trade_count" in data[0]
        assert "volume_usd" in data[0]


@pytest.mark.asyncio
async def test_get_market_signals(client):
    resp = await client.get("/api/markets/mkt-001/signals")
    assert resp.status_code == 200
    data = resp.json()
    for sig in data:
        assert sig["market_id"] == "mkt-001"
