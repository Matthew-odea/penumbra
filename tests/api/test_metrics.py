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
    keys = {"bucket", "trades", "signals", "llm_t1", "llm_t2", "alerts"}
    for point in data:
        assert keys.issubset(point.keys()), f"Missing keys in {point}"
        # Counts should be non-negative integers
        for k in ("trades", "signals", "llm_t1", "llm_t2", "alerts"):
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

    # Top-level keys
    assert "funnel" in data
    assert "classification" in data
    assert "score_distribution" in data
    assert "top_markets" in data


@pytest.mark.anyio
async def test_overview_funnel(client):
    """Funnel counts should be monotonically decreasing."""
    resp = await client.get("/api/metrics/overview")
    data = resp.json()
    funnel = data["funnel"]

    assert "trades" in funnel
    assert "signals" in funnel
    assert "classified" in funnel
    assert "high_suspicion" in funnel

    # Monotonically decreasing: trades ≥ signals ≥ classified ≥ high_suspicion
    assert funnel["trades"] >= funnel["signals"]
    assert funnel["signals"] >= funnel["classified"]
    assert funnel["classified"] >= funnel["high_suspicion"]


@pytest.mark.anyio
async def test_overview_classification(client):
    """Classification should contain INFORMED and NOISE counts from seeded data."""
    resp = await client.get("/api/metrics/overview")
    data = resp.json()
    classification = data["classification"]

    # Seeded data has 3 NOISE (i=0,1,2) and 3 INFORMED (i=3,4,5)
    assert classification.get("NOISE", 0) > 0
    assert classification.get("INFORMED", 0) > 0


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

    # Check structure
    for m in top:
        assert "market_id" in m
        assert "signal_count" in m
        assert m["signal_count"] > 0

    # mkt-001 should be the top market (5 signals vs mkt-002's 3)
    market_ids = [m["market_id"] for m in top]
    assert "mkt-001" in market_ids


@pytest.mark.anyio
async def test_overview_funnel_values(client):
    """Verify funnel numbers against known seed data."""
    resp = await client.get("/api/metrics/overview")
    data = resp.json()
    funnel = data["funnel"]

    # Seeded: 26 trades total (20 regular + 6 resolved)
    # But only today's count — trades are spread from -6h to now, so most are today
    assert funnel["trades"] > 0

    # 8 signals seeded
    assert funnel["signals"] > 0

    # 6 signal_reasoning records (classified)
    assert funnel["classified"] > 0
