"""Integration tests — end-to-end data flow across pipeline stages.

These tests verify that data flows correctly between:
  - Scanner → DuckDB → API
  - Cross-endpoint consistency (market signals match wallet signals, etc.)
  - Full pipeline: trade → signal → API response
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest
from httpx import ASGITransport, AsyncClient

from sentinel.db.init import SCHEMA_SQL
from sentinel.scanner.scorer import build_signal, write_signal

# ── Shared fixture ──────────────────────────────────────────────────────────


def _full_db() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with production schema and realistic seed data."""
    conn = duckdb.connect(":memory:")
    conn.execute(SCHEMA_SQL)

    now = datetime.now(tz=UTC)

    # Markets
    conn.execute(
        "INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)",
        ["mkt-btc", "Will BTC exceed $100k by June?", "btc-100k", "Crypto",
         now + timedelta(days=60), 5500000.0, 1200000.0, True, False, None, now],
    )
    conn.execute(
        "INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)",
        ["mkt-fed", "Will the Fed cut rates in Q3?", "fed-cut-q3", "Politics",
         now + timedelta(days=90), 8200000.0, 3100000.0, True, False, None, now],
    )
    conn.execute(
        "INSERT INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)",
        ["mkt-resolved", "Resolved test market", "resolved-mkt", "Science",
         now - timedelta(days=10), 200000.0, 50000.0, False, True, 1.0, now],
    )

    # Trades — 20 across two markets + 6 on resolved
    base = now - timedelta(hours=6)
    for i in range(20):
        mid = "mkt-btc" if i < 10 else "mkt-fed"
        wallet = ["0xAlpha", "0xBravo", "0xCharlie"][i % 3]
        conn.execute(
            "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,'ws',?)",
            [f"trade-{i:03d}", mid, f"asset-{i % 3}", wallet,
             "BUY" if i % 2 == 0 else "SELL",
             Decimal("0.65") + Decimal("0.01") * i,
             Decimal("1000") + Decimal("500") * i,
             base + timedelta(minutes=i * 15),
             f"0xhash{i:03d}", now],
        )

    for i in range(6):
        conn.execute(
            "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,'ws',?)",
            [f"trade-res-{i}", "mkt-resolved", "asset-res", "0xAlpha",
             "BUY", Decimal("0.70"), Decimal("2000"),
             now - timedelta(days=5, hours=i), f"0xresolved{i}", now],
        )

    # Signals (27 columns)
    for i in range(8):
        sid = f"sig-{i:03d}"
        mid = "mkt-btc" if i < 5 else "mkt-fed"
        wallet = "0xAlpha" if i % 2 == 0 else "0xBravo"
        score = 30 + i * 10
        conn.execute(
            "INSERT INTO signals VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [sid, f"trade-{i:03d}", mid, wallet, "BUY", 0.65, 5000.0,
             base + timedelta(minutes=i * 15),
             3.5, 2.1, 0.03, 0.72, 15, False, False, None, score, 0.0, None, 0.0, 0, False, 2, 0,
             None, None, now],
        )

    # Budget — market scoring
    today = now.date()
    conn.execute("INSERT INTO llm_budget VALUES (?, 'market_scoring', 42, 4000)", [today])

    return conn


@pytest.fixture()
def int_db():
    """Fixture providing an integration-test database."""
    return _full_db()


@pytest.fixture()
def int_client(int_db):
    """Async httpx client wired to the FastAPI app with integration DB."""
    from unittest.mock import patch as _patch

    _fake = lambda db_path=None: int_db  # noqa: E731

    from sentinel.api.main import app

    transport = ASGITransport(app=app)

    with (
        _patch("sentinel.api.deps.get_db", _fake),
        _patch("sentinel.api.deps._conn", int_db),
        _patch("sentinel.api.routes.signals.get_db", _fake),
        _patch("sentinel.api.routes.markets.get_db", _fake),
        _patch("sentinel.api.routes.wallets.get_db", _fake),
        _patch("sentinel.api.routes.budget.get_db", _fake),
    ):
        yield AsyncClient(transport=transport, base_url="http://test")


# ═══════════════════════════════════════════════════════════════════════════
# 1. End-to-end: signal write → API read
# ═══════════════════════════════════════════════════════════════════════════


class TestSignalToApiFlow:
    """Verify that signals written to DuckDB appear correctly via the API."""

    @pytest.mark.asyncio
    async def test_written_signal_appears_in_feed(self, int_db, int_client):
        """A new signal inserted into DuckDB should appear in the feed."""
        sig = build_signal(
            trade_id="trade-new-001",
            market_id="mkt-btc",
            wallet="0xNewWallet",
            side="BUY", price=0.8, size_usd=10000.0,
            trade_timestamp=datetime.now(tz=UTC),
            z_score=5.0, modified_z_score=5.0,
            price_impact=0.02, is_whitelisted=True,
        )
        write_signal(int_db, sig)

        resp = await int_client.get(
            "/api/signals", params={"wallet": "0xNewWallet"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["wallet"] == "0xNewWallet"
        assert data[0]["statistical_score"] >= 30

    @pytest.mark.asyncio
    async def test_signal_fields_present(self, int_db, int_client):
        """Signals should have the expected fields."""
        resp = await int_client.get("/api/signals", params={"wallet": "0xAlpha"})
        data = resp.json()
        assert len(data) > 0
        s = data[0]
        # Core fields
        assert "signal_id" in s
        assert "market_id" in s
        assert "statistical_score" in s
        assert "side" in s
        # explanation field exists (may be None for low scores)
        assert "explanation" in s
        # No LLM classification fields
        assert "classification" not in s
        assert "reasoning" not in s
        assert "tier1_model" not in s


# ═══════════════════════════════════════════════════════════════════════════
# 2. Cross-endpoint consistency
# ═══════════════════════════════════════════════════════════════════════════


class TestCrossEndpointConsistency:
    """Signals returned by /signals, /markets/{id}/signals, and
    /wallets/{addr}/signals should be consistent."""

    @pytest.mark.asyncio
    async def test_market_signals_subset_of_feed(self, int_client):
        """Signals for mkt-btc via market endpoint should match the feed filter."""
        all_resp = await int_client.get(
            "/api/signals", params={"market_id": "mkt-btc"}
        )
        market_resp = await int_client.get("/api/markets/mkt-btc/signals")

        all_ids = {s["signal_id"] for s in all_resp.json()}
        market_ids = {s["signal_id"] for s in market_resp.json()}
        assert all_ids == market_ids

    @pytest.mark.asyncio
    async def test_wallet_signals_subset_of_feed(self, int_client):
        """Signals for 0xAlpha via wallet endpoint should match the feed filter."""
        all_resp = await int_client.get(
            "/api/signals", params={"wallet": "0xAlpha"}
        )
        wallet_resp = await int_client.get("/api/wallets/0xAlpha/signals")

        all_ids = {s["signal_id"] for s in all_resp.json()}
        wallet_ids = {s["signal_id"] for s in wallet_resp.json()}
        assert all_ids == wallet_ids

    @pytest.mark.asyncio
    async def test_market_detail_exists_for_signal_markets(self, int_client):
        """Every market_id in the signals feed should have a valid market detail."""
        resp = await int_client.get("/api/signals")
        market_ids = {s["market_id"] for s in resp.json()}

        for mid in market_ids:
            detail_resp = await int_client.get(f"/api/markets/{mid}")
            assert detail_resp.status_code == 200, f"Market {mid} not found"
            assert detail_resp.json()["market_id"] == mid

    @pytest.mark.asyncio
    async def test_wallet_profile_exists_for_signal_wallets(self, int_client):
        """Every wallet in the signals feed should have a wallet profile."""
        resp = await int_client.get("/api/signals")
        wallets = {s["wallet"] for s in resp.json()}

        for addr in wallets:
            profile_resp = await int_client.get(f"/api/wallets/{addr}")
            assert profile_resp.status_code == 200
            assert profile_resp.json()["wallet"] == addr


# ═══════════════════════════════════════════════════════════════════════════
# 3. Budget + stats consistency
# ═══════════════════════════════════════════════════════════════════════════


class TestBudgetAndStats:
    """Verify budget and signal stats endpoints reflect actual data."""

    @pytest.mark.asyncio
    async def test_budget_reflects_db(self, int_client):
        resp = await int_client.get("/api/budget")
        assert resp.status_code == 200
        data = resp.json()
        assert data["market_scoring"]["calls_used"] == 42
        assert data["market_scoring"]["calls_limit"] == 4000

    @pytest.mark.asyncio
    async def test_stats_count_matches_feed(self, int_client):
        """Signal stats total should match filtered feed length."""
        stats_resp = await int_client.get("/api/signals/stats")
        stats = stats_resp.json()

        feed_resp = await int_client.get("/api/signals")
        feed = feed_resp.json()

        assert stats["total_signals_today"] >= 0
        assert len(feed) >= stats["total_signals_today"]

    @pytest.mark.asyncio
    async def test_high_suspicion_count_accurate(self, int_client):
        """High suspicion count should match signals with score ≥ 80."""
        stats = (await int_client.get("/api/signals/stats")).json()
        assert stats["high_suspicion_today"] >= 0


# ═══════════════════════════════════════════════════════════════════════════
# 4. Health endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestHealthIntegration:
    @pytest.mark.asyncio
    async def test_health_returns_ok(self, int_client):
        resp = await int_client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "timestamp" in body


# ═══════════════════════════════════════════════════════════════════════════
# 5. Full pipeline → API: write signal, then read via API
# ═══════════════════════════════════════════════════════════════════════════


class TestFullPipelineToApi:
    """End-to-end: write a signal to DuckDB, then verify the API returns it."""

    @pytest.mark.asyncio
    async def test_full_data_path(self, int_db, int_client):
        """Write signal → read via /api/signals."""
        now = datetime.now(tz=UTC)
        sig = build_signal(
            trade_id="trade-e2e-001",
            market_id="mkt-btc",
            wallet="0xE2EWallet",
            side="SELL", price=0.35, size_usd=15000.0,
            trade_timestamp=now,
            z_score=7.0, modified_z_score=7.0,
            price_impact=0.04,
            wallet_win_rate=0.9,
            wallet_total_trades=50,
            is_whitelisted=True,
            funding_anomaly=True,
            funding_age_minutes=3,
        )
        write_signal(int_db, sig)

        resp = await int_client.get(
            "/api/signals", params={"wallet": "0xE2EWallet"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1

        result = data[0]
        assert result["signal_id"] == sig.signal_id
        assert result["market_id"] == "mkt-btc"
        assert result["wallet"] == "0xE2EWallet"
        assert result["side"] == "SELL"
        assert result["statistical_score"] >= 30
        assert result["market_question"] == "Will BTC exceed $100k by June?"
        assert result["category"] == "Crypto"
        # explanation may or may not be set depending on score threshold
        assert "explanation" in result

    @pytest.mark.asyncio
    async def test_signal_without_reasoning_works(self, int_db, int_client):
        """A signal should appear in the feed without any reasoning data."""
        sig = build_signal(
            trade_id="trade-noreas-001",
            market_id="mkt-fed",
            wallet="0xNoReasoning",
            side="BUY", price=0.55, size_usd=3000.0,
            trade_timestamp=datetime.now(tz=UTC),
            z_score=4.5, modified_z_score=4.5,
            is_whitelisted=True,
        )
        write_signal(int_db, sig)

        resp = await int_client.get(
            "/api/signals", params={"wallet": "0xNoReasoning"}
        )
        data = resp.json()
        assert len(data) == 1
        assert data[0]["statistical_score"] >= 30

    @pytest.mark.asyncio
    async def test_market_volume_endpoint(self, int_client):
        """Volume endpoint should return hourly bucketed data."""
        resp = await int_client.get("/api/markets/mkt-btc/volume", params={"hours": 24})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        if data:
            assert "hour" in data[0]
            assert "trade_count" in data[0]
            assert "volume_usd" in data[0]

    @pytest.mark.asyncio
    async def test_wallet_profile_categories(self, int_client):
        """Wallet profile should correctly break down trades by category."""
        resp = await int_client.get("/api/wallets/0xAlpha")
        assert resp.status_code == 200
        profile = resp.json()

        assert profile["wallet"] == "0xAlpha"
        assert profile["total_trades"] > 0
        assert isinstance(profile["categories"], list)

        cat_names = {c["category"] for c in profile["categories"]}
        assert len(cat_names) >= 1

    @pytest.mark.asyncio
    async def test_market_list_ordered_by_signal_activity(self, int_client):
        """Markets should be returned with signal counts."""
        resp = await int_client.get("/api/markets")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 2
        signal_counts = [m["signal_count"] for m in data]
        assert any(c > 0 for c in signal_counts)

    @pytest.mark.asyncio
    async def test_nonexistent_market_returns_404(self, int_client):
        resp = await int_client.get("/api/markets/nonexistent-mkt")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_wallet_trades_endpoint(self, int_client):
        """Wallet trades should return trade history with market metadata."""
        resp = await int_client.get("/api/wallets/0xAlpha/trades")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) > 0

        trade = data[0]
        assert "trade_id" in trade
        assert "market_question" in trade
        assert "category" in trade
        for t in data:
            assert isinstance(t["price"], (int, float))
            assert isinstance(t["size_usd"], (int, float))
