"""Tests for /api/budget endpoint."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_get_budget(client):
    resp = await client.get("/api/budget")
    assert resp.status_code == 200
    body = resp.json()
    assert "date" in body
    assert "market_scoring" in body


@pytest.mark.asyncio
async def test_budget_market_scoring_values(client):
    resp = await client.get("/api/budget")
    body = resp.json()
    ms = body["market_scoring"]
    assert ms["calls_used"] == 42
    assert ms["calls_limit"] == 4000
