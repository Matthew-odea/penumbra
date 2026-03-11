"""Tests for /api/budget endpoint."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_get_budget(client):
    resp = await client.get("/api/budget")
    assert resp.status_code == 200
    body = resp.json()
    assert "date" in body
    assert "tier1" in body
    assert "tier2" in body


@pytest.mark.asyncio
async def test_budget_tier1_values(client):
    resp = await client.get("/api/budget")
    body = resp.json()
    t1 = body["tier1"]
    assert t1["calls_used"] == 42
    assert t1["calls_limit"] == 200


@pytest.mark.asyncio
async def test_budget_tier2_values(client):
    resp = await client.get("/api/budget")
    body = resp.json()
    t2 = body["tier2"]
    assert t2["calls_used"] == 7
    assert t2["calls_limit"] == 30
