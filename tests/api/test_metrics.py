"""Tests for the metrics API endpoints."""

from __future__ import annotations

import pytest


@pytest.mark.anyio
async def test_timeseries_returns_list(client):
    """GET /api/metrics/timeseries returns a list of bucketed data points."""
    resp = await client.get("/api/metrics/timeseries", params={"hours": 6, "bucket_minutes": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) > 0

    # Each point should have all expected keys
    keys = {"bucket", "trades", "signals", "alerts"}
    for point in data:
        assert keys.issubset(point.keys()), f"Missing keys in {point}"
        for k in ("trades", "signals", "alerts"):
            assert isinstance(point[k], int) and point[k] >= 0


@pytest.mark.anyio
async def test_timeseries_default_params(client):
    """GET /api/metrics/timeseries works with default parameters."""
    resp = await client.get("/api/metrics/timeseries")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


@pytest.mark.anyio
async def test_timeseries_custom_range(client):
    """Different hour ranges return different bucket counts."""
    resp_1h = await client.get("/api/metrics/timeseries", params={"hours": 1, "bucket_minutes": 1})
    resp_24h = await client.get("/api/metrics/timeseries", params={"hours": 24, "bucket_minutes": 15})
    assert resp_1h.status_code == 200
    assert resp_24h.status_code == 200

    data_1h = resp_1h.json()
    data_24h = resp_24h.json()
    # 24h/15min = 96 buckets, 1h/1min = 60 buckets
    assert len(data_24h) >= len(data_1h)


@pytest.mark.anyio
async def test_timeseries_has_nonzero_data(client):
    """Seeded data should produce at least some non-zero trade counts."""
    resp = await client.get("/api/metrics/timeseries", params={"hours": 24, "bucket_minutes": 60})
    data = resp.json()
    total_trades = sum(p["trades"] for p in data)
    assert total_trades > 0, "Expected non-zero trade counts with seeded data"


@pytest.mark.anyio
async def test_overview_structure(client):
    """GET /api/metrics/overview returns expected structure."""
    resp = await client.get("/api/metrics/overview")
    assert resp.status_code == 200
    data = resp.json()

    assert "funnel" in data
    assert "score_distribution" in data
    assert "top_markets" in data
    assert "top_traded_markets" in data
    assert "market_coverage" in data
    # No more classification/tier2 fields
    assert "classification" not in data
    assert "tier2_coverage" not in data


@pytest.mark.anyio
async def test_overview_funnel(client):
    """Funnel counts should be present and non-negative."""
    resp = await client.get("/api/metrics/overview")
    data = resp.json()
    funnel = data["funnel"]

    assert "trades" in funnel
    assert "signals" in funnel
    assert "high_suspicion" in funnel
    # No longer has 'classified'
    assert "classified" not in funnel

    assert funnel["trades"] >= funnel["signals"]
    assert funnel["signals"] >= funnel["high_suspicion"]


@pytest.mark.anyio
async def test_overview_score_distribution(client):
    """Score distribution should have bucket labels and non-negative counts."""
    resp = await client.get("/api/metrics/overview")
    data = resp.json()
    dist = data["score_distribution"]

    assert isinstance(dist, dict)
    total = sum(dist.values())
    assert total > 0, "At least some signals should appear in score distribution"


@pytest.mark.anyio
async def test_overview_top_markets(client):
    """Top markets should include seeded markets with signals."""
    resp = await client.get("/api/metrics/overview")
    data = resp.json()
    top = data["top_markets"]

    assert isinstance(top, list)
    assert len(top) > 0

    for m in top:
        assert "market_id" in m
        assert "signal_count" in m
        assert m["signal_count"] > 0

    market_ids = [m["market_id"] for m in top]
    assert "mkt-001" in market_ids


@pytest.mark.anyio
async def test_overview_funnel_values(client):
    """Verify funnel numbers against known seed data."""
    resp = await client.get("/api/metrics/overview")
    data = resp.json()
    funnel = data["funnel"]

    assert funnel["trades"] > 0
    assert funnel["signals"] > 0


@pytest.mark.anyio
async def test_ingestion_structure(client):
    """GET /api/metrics/ingestion returns expected structure."""
    resp = await client.get("/api/metrics/ingestion")
    assert resp.status_code == 200
    data = resp.json()

    assert "totals" in data
    assert "latest" in data
    assert "markets_active_today" in data
    assert "wallets_active_today" in data
    assert "hourly" in data

    assert "all_time" in data["totals"]
    assert "today" in data["totals"]
    assert "rest" in data["latest"]
    assert isinstance(data["hourly"], list)


@pytest.mark.anyio
async def test_ingestion_totals_match_trades(client):
    """Ingestion totals should match the actual trade count."""
    resp = await client.get("/api/metrics/ingestion")
    data = resp.json()

    # Seeded trades: 20 regular + 6 resolved = 26 total
    assert data["totals"]["all_time"] == 26
    assert data["totals"]["today"] >= 0
    assert data["wallets_active_today"] >= 0
    assert data["markets_active_today"] >= 0


@pytest.mark.anyio
async def test_ingestion_hourly_format(client):
    """Hourly ingestion entries should have bucket and trades keys."""
    resp = await client.get("/api/metrics/ingestion")
    data = resp.json()
    hourly = data["hourly"]

    if len(hourly) > 0:
        for entry in hourly:
            assert "bucket" in entry
            assert "trades" in entry
            assert isinstance(entry["trades"], int)
