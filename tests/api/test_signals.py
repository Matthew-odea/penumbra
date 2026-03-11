"""Tests for /api/signals endpoints."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_list_signals_returns_data(client):
    resp = await client.get("/api/signals")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) > 0


@pytest.mark.asyncio
async def test_list_signals_has_expected_fields(client):
    resp = await client.get("/api/signals")
    data = resp.json()
    sig = data[0]
    for field in (
        "signal_id", "trade_id", "market_id", "wallet", "side",
        "price", "size_usd", "statistical_score", "market_question",
    ):
        assert field in sig, f"Missing field: {field}"


@pytest.mark.asyncio
async def test_list_signals_filter_by_min_score(client):
    resp = await client.get("/api/signals", params={"min_score": 80})
    assert resp.status_code == 200
    data = resp.json()
    for sig in data:
        effective_score = sig["suspicion_score"] or sig["statistical_score"]
        assert effective_score >= 80


@pytest.mark.asyncio
async def test_list_signals_filter_by_market(client):
    resp = await client.get("/api/signals", params={"market_id": "mkt-001"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) > 0
    for sig in data:
        assert sig["market_id"] == "mkt-001"


@pytest.mark.asyncio
async def test_list_signals_filter_by_wallet(client):
    resp = await client.get("/api/signals", params={"wallet": "0xAlpha"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) > 0
    for sig in data:
        assert sig["wallet"] == "0xAlpha"


@pytest.mark.asyncio
async def test_list_signals_respects_limit(client):
    resp = await client.get("/api/signals", params={"limit": 3})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) <= 3


@pytest.mark.asyncio
async def test_signals_ordered_by_created_at_desc(client):
    resp = await client.get("/api/signals")
    data = resp.json()
    timestamps = [s["created_at"] for s in data if s["created_at"]]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.asyncio
async def test_signal_stats(client):
    resp = await client.get("/api/signals/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "total_signals_today" in body
    assert "high_suspicion_today" in body
    assert "active_markets" in body
    assert isinstance(body["total_signals_today"], int)
