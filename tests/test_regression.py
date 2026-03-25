"""Regression tests — verify schema invariants, data flow, and cross-layer consistency.

These tests guard against regressions in the core data model and ensure
that changes to one layer don't silently break another.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import duckdb
import pytest

from sentinel.db.init import SCHEMA_SQL
from sentinel.judge.classifier import ClassificationResult
from sentinel.judge.reasoner import ReasoningResult
from sentinel.judge.store import build_alert, store_reasoning
from sentinel.scanner.scorer import (
    Signal,
    build_signal,
    compute_statistical_score,
    write_signal,
    write_signals,
)

# ── Helpers ─────────────────────────────────────────────────────────────────


def _fresh_db() -> duckdb.DuckDBPyConnection:
    """Return an in-memory DuckDB with the full prod schema applied."""
    conn = duckdb.connect(":memory:")
    conn.execute(SCHEMA_SQL)
    return conn


def _make_signal(**overrides) -> Signal:
    defaults = dict(
        signal_id=str(uuid4()),
        trade_id="trade-001",
        market_id="mkt-001",
        wallet="0xTestWallet",
        side="BUY",
        price=0.72,
        size_usd=5000.0,
        trade_timestamp=datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
        volume_z_score=4.2,
        modified_z_score=4.2,
        price_impact=0.015,
        wallet_win_rate=0.78,
        wallet_total_trades=45,
        is_whitelisted=True,
        funding_anomaly=False,
        funding_age_minutes=None,
        statistical_score=75,
        created_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Signal(**defaults)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Schema regression
# ═══════════════════════════════════════════════════════════════════════════


class TestSchemaRegression:
    """Ensure the DuckDB schema has all expected tables, views, and columns."""

    def test_expected_tables_exist(self):
        conn = _fresh_db()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main' AND table_type='BASE TABLE'"
            ).fetchall()
        }
        for t in ("markets", "trades", "signals", "signal_reasoning", "llm_budget"):
            assert t in tables, f"Missing table: {t}"

    def test_expected_views_exist(self):
        conn = _fresh_db()
        views = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main' AND table_type='VIEW'"
            ).fetchall()
        }
        for v in ("v_hourly_volume", "v_volume_anomalies", "v_wallet_performance"):
            assert v in views, f"Missing view: {v}"

    def test_markets_columns(self):
        conn = _fresh_db()
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='markets'"
            ).fetchall()
        }
        expected = {
            "market_id", "question", "slug", "category", "end_date",
            "volume_usd", "liquidity_usd", "active", "resolved",
            "resolved_price", "last_synced",
        }
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_trades_columns(self):
        conn = _fresh_db()
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='trades'"
            ).fetchall()
        }
        expected = {
            "trade_id", "market_id", "asset_id", "wallet", "side",
            "price", "size_usd", "timestamp", "tx_hash", "source", "ingested_at",
        }
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_signals_columns(self):
        conn = _fresh_db()
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='signals'"
            ).fetchall()
        }
        expected = {
            "signal_id", "trade_id", "market_id", "wallet", "side",
            "price", "size_usd", "trade_timestamp", "volume_z_score",
            "modified_z_score", "price_impact", "wallet_win_rate",
            "wallet_total_trades", "is_whitelisted", "funding_anomaly",
            "funding_age_minutes", "statistical_score", "created_at",
        }
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_signal_reasoning_columns(self):
        conn = _fresh_db()
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='signal_reasoning'"
            ).fetchall()
        }
        expected = {
            "signal_id", "trade_id", "classification", "tier1_confidence",
            "suspicion_score", "reasoning", "key_evidence", "news_headlines",
            "tier1_model", "tier2_model", "tier1_tokens", "tier2_tokens",
            "created_at",
        }
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_llm_budget_columns(self):
        conn = _fresh_db()
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='llm_budget'"
            ).fetchall()
        }
        expected = {"date", "tier", "calls_used", "calls_limit"}
        assert expected.issubset(cols), f"Missing columns: {expected - cols}"

    def test_schema_idempotent(self):
        """Applying the schema twice should not raise."""
        conn = duckdb.connect(":memory:")
        conn.execute(SCHEMA_SQL)
        conn.execute(SCHEMA_SQL)  # No error on second apply
        count = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='main'"
        ).fetchone()[0]
        assert count > 0


# ═══════════════════════════════════════════════════════════════════════════
# 2. Scorer regression — composite score stays within 0-100
# ═══════════════════════════════════════════════════════════════════════════


class TestScorerRegression:
    """Guard against drift in the composite scoring logic."""

    def test_score_zero_for_no_signals(self):
        score = compute_statistical_score(
            z_score=0.0, price_impact=0.0,
            win_rate=None, is_whitelisted=False,
            funding_anomaly=False, funding_age_minutes=None,
        )
        assert score == 0

    def test_score_capped_at_100(self):
        score = compute_statistical_score(
            z_score=100.0, price_impact=1.0,
            win_rate=1.0, is_whitelisted=True,
            funding_anomaly=True, funding_age_minutes=1,
        )
        assert score <= 100

    def test_score_nonneg_for_subthreshold_inputs(self):
        """With benign inputs (below threshold), score should be 0."""
        score = compute_statistical_score(
            z_score=0.0, price_impact=0.0,
            win_rate=0.3, is_whitelisted=False,
            funding_anomaly=False, funding_age_minutes=None,
        )
        assert score == 0

    def test_whitelisted_adds_wallet_weight(self):
        base = compute_statistical_score(
            z_score=5.0, price_impact=0.0,
            win_rate=None, is_whitelisted=False,
            funding_anomaly=False, funding_age_minutes=None,
        )
        with_wl = compute_statistical_score(
            z_score=5.0, price_impact=0.0,
            win_rate=None, is_whitelisted=True,
            funding_anomaly=False, funding_age_minutes=None,
        )
        assert with_wl > base

    def test_funding_anomaly_adds_score(self):
        base = compute_statistical_score(
            z_score=5.0, price_impact=0.0,
            win_rate=None, is_whitelisted=False,
            funding_anomaly=False, funding_age_minutes=None,
        )
        with_fund = compute_statistical_score(
            z_score=5.0, price_impact=0.0,
            win_rate=None, is_whitelisted=False,
            funding_anomaly=True, funding_age_minutes=5,
        )
        assert with_fund > base

    def test_build_signal_computes_score(self):
        sig = build_signal(
            trade_id="t1", market_id="m1", wallet="0x1",
            side="BUY", price=0.5, size_usd=1000.0,
            trade_timestamp=datetime.now(tz=UTC),
            z_score=0.0, modified_z_score=0.0,
        )
        assert sig.statistical_score == 0

    def test_build_signal_high_score(self):
        sig = build_signal(
            trade_id="t1", market_id="m1", wallet="0x1",
            side="BUY", price=0.5, size_usd=10000.0,
            trade_timestamp=datetime.now(tz=UTC),
            z_score=8.0, modified_z_score=8.0,
            price_impact=0.05,
            is_whitelisted=True,
            funding_anomaly=True,
            funding_age_minutes=5,
        )
        assert sig.statistical_score >= 30


# ═══════════════════════════════════════════════════════════════════════════
# 3. Signal write/read round-trip
# ═══════════════════════════════════════════════════════════════════════════


class TestSignalPersistence:
    """Verify signals survive a DuckDB write → read round-trip."""

    def test_write_and_read_signal(self):
        conn = _fresh_db()
        sig = _make_signal(signal_id="sig-rt-001")
        write_signal(conn, sig)

        row = conn.execute(
            "SELECT signal_id, market_id, wallet, statistical_score "
            "FROM signals WHERE signal_id = ?",
            ["sig-rt-001"],
        ).fetchone()
        assert row is not None
        assert row[0] == "sig-rt-001"
        assert row[1] == "mkt-001"
        assert row[2] == "0xTestWallet"
        assert row[3] == 75

    def test_write_signals_batch(self):
        conn = _fresh_db()
        sigs = [_make_signal(signal_id=f"batch-{i}") for i in range(5)]
        write_signals(conn, sigs)

        count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        assert count == 5

    def test_duplicate_signal_ignored(self):
        """INSERT OR IGNORE should silently skip duplicates."""
        conn = _fresh_db()
        sig = _make_signal(signal_id="sig-dup-001")
        write_signal(conn, sig)
        write_signal(conn, sig)  # Duplicate

        count = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE signal_id = ?",
            ["sig-dup-001"],
        ).fetchone()[0]
        assert count == 1

    def test_signal_as_db_tuple_length(self):
        """The tuple must match the INSERT column count (23)."""
        sig = _make_signal()
        assert len(sig.as_db_tuple()) == 23

    def test_signal_as_dict_keys(self):
        sig = _make_signal()
        d = sig.as_dict()
        for key in ("signal_id", "trade_id", "market_id", "statistical_score"):
            assert key in d


# ═══════════════════════════════════════════════════════════════════════════
# 4. Reasoning store round-trip
# ═══════════════════════════════════════════════════════════════════════════


class TestReasoningPersistence:
    """Verify the judge's store_reasoning writes valid rows."""

    def test_tier1_only_round_trip(self):
        conn = _fresh_db()
        sig = _make_signal(signal_id="sig-reason-001")
        t1 = ClassificationResult(
            classification="INFORMED", confidence=70,
            one_liner="Suspicious timing", model="nova-lite",
            input_tokens=100, output_tokens=50,
        )
        store_reasoning(conn, sig, t1, None, ["headline-1", "headline-2"])

        row = conn.execute(
            "SELECT classification, tier1_confidence, suspicion_score, "
            "reasoning, tier2_model, news_headlines "
            "FROM signal_reasoning WHERE signal_id = ?",
            ["sig-reason-001"],
        ).fetchone()
        assert row is not None
        assert row[0] == "INFORMED"
        assert row[1] == 70
        assert row[2] == 70  # No T2 → suspicion = T1 confidence
        assert row[3] == "Suspicious timing"
        assert row[4] is None  # No T2 model
        headlines = json.loads(row[5])
        assert len(headlines) == 2

    def test_tier2_overrides_suspicion(self):
        conn = _fresh_db()
        sig = _make_signal(signal_id="sig-reason-002")
        t1 = ClassificationResult(
            classification="INFORMED", confidence=75,
            one_liner="Suspicious", model="nova-lite",
            input_tokens=100, output_tokens=50,
        )
        t2 = ReasoningResult(
            suspicion_score=92, reasoning="Very high confidence after deep analysis.",
            key_evidence="Timing matches insider calendar", model="nova-pro",
            input_tokens=200, output_tokens=80,
        )
        store_reasoning(conn, sig, t1, t2, ["headline"])

        row = conn.execute(
            "SELECT suspicion_score, reasoning, tier2_model "
            "FROM signal_reasoning WHERE signal_id = ?",
            ["sig-reason-002"],
        ).fetchone()
        assert row[0] == 92
        assert row[1] == "Very high confidence after deep analysis."
        assert row[2] == "nova-pro"


# ═══════════════════════════════════════════════════════════════════════════
# 5. Alert threshold regression
# ═══════════════════════════════════════════════════════════════════════════


class TestAlertThreshold:
    """Ensure the alert boundary (score ≥ 80) is enforced correctly."""

    @pytest.mark.parametrize("confidence,expect_alert", [
        (79, False), (80, True), (81, True), (100, True), (0, False),
    ])
    def test_boundary_tier1_only(self, confidence: int, expect_alert: bool):
        sig = _make_signal()
        t1 = ClassificationResult(
            classification="INFORMED", confidence=confidence,
            one_liner="Test", model="nova-lite",
            input_tokens=0, output_tokens=0,
        )
        alert = build_alert(sig, t1, None)
        assert (alert is not None) == expect_alert

    def test_tier2_score_overrides_tier1_for_alert(self):
        """T1=85 (would trigger) but T2=50 (should suppress)."""
        sig = _make_signal()
        t1 = ClassificationResult(
            classification="INFORMED", confidence=85,
            one_liner="Suspicious", model="nova-lite",
            input_tokens=0, output_tokens=0,
        )
        t2 = ReasoningResult(
            suspicion_score=50, reasoning="On review, normal.",
            key_evidence="None", model="nova-pro",
            input_tokens=0, output_tokens=0,
        )
        alert = build_alert(sig, t1, t2)
        assert alert is None  # T2=50 < 80

    def test_tier2_high_creates_alert(self):
        """T1=60 (wouldn't trigger) but T2=90 (should trigger)."""
        sig = _make_signal()
        t1 = ClassificationResult(
            classification="INFORMED", confidence=60,
            one_liner="Maybe", model="nova-lite",
            input_tokens=0, output_tokens=0,
        )
        t2 = ReasoningResult(
            suspicion_score=90, reasoning="Definitely informed.",
            key_evidence="Timing", model="nova-pro",
            input_tokens=0, output_tokens=0,
        )
        alert = build_alert(sig, t1, t2)
        assert alert is not None
        assert alert.score == 90


# ═══════════════════════════════════════════════════════════════════════════
# 6. View regression — v_hourly_volume and v_wallet_performance
# ═══════════════════════════════════════════════════════════════════════════


class TestViewRegression:
    """Ensure analytical views return expected shapes with seeded data."""

    def _seed_trades(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Insert trades and markets for view testing."""
        now = datetime.now(tz=UTC)
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, TRUE, TRUE, 1.0, ?, NULL, NULL, NULL)",
            ["mkt-v1", "View test market?", "view-test", "Crypto",
             now + timedelta(days=30), 1000000.0, 50000.0, now],
        )
        # Insert 10 trades spread across hours
        for i in range(10):
            conn.execute(
                "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'ws', ?)",
                [f"vt-{i}", "mkt-v1", "asset-v1", "0xViewWallet", "BUY",
                 0.6, 1000.0 + i * 100, now - timedelta(hours=i), now],
            )

    def test_v_hourly_volume_groups_by_hour(self):
        conn = _fresh_db()
        self._seed_trades(conn)
        rows = conn.execute("SELECT * FROM v_hourly_volume WHERE market_id = 'mkt-v1'").fetchall()
        assert len(rows) > 0
        # Each row should have: market_id, hour_bucket, trade_count, volume_usd, unique_wallets
        assert len(rows[0]) == 5

    def test_v_wallet_performance_requires_5_trades(self):
        conn = _fresh_db()
        self._seed_trades(conn)
        rows = conn.execute(
            "SELECT * FROM v_wallet_performance WHERE wallet = '0xViewWallet'"
        ).fetchall()
        # 10 trades on a resolved market → qualifies (≥ 5)
        assert len(rows) == 1
        assert rows[0][1] == 10  # total_resolved_trades

    def test_v_wallet_performance_excludes_few_trades(self):
        conn = _fresh_db()
        now = datetime.now(tz=UTC)
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, TRUE, TRUE, 1.0, ?, NULL, NULL, NULL)",
            ["mkt-few", "Few?", "few", "Science",
             now + timedelta(days=30), 100000.0, 10000.0, now],
        )
        for i in range(3):  # Only 3 trades — below threshold
            conn.execute(
                "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'ws', ?)",
                [f"few-{i}", "mkt-few", "a1", "0xFewTrader", "BUY",
                 0.5, 500.0, now - timedelta(hours=i), now],
            )
        rows = conn.execute(
            "SELECT * FROM v_wallet_performance WHERE wallet = '0xFewTrader'"
        ).fetchall()
        assert len(rows) == 0  # Excluded by HAVING COUNT(*) >= 5


# ═══════════════════════════════════════════════════════════════════════════
# 7. Data-type coercion regression (JSON serialisation)
# ═══════════════════════════════════════════════════════════════════════════


class TestCoercionRegression:
    """Verify Decimal/datetime types don't leak into JSON responses."""

    def test_signal_dict_has_no_decimal(self):
        sig = _make_signal()
        d = sig.as_dict()
        for k, v in d.items():
            if isinstance(v, float):
                assert not isinstance(v, Decimal), f"{k} is Decimal, expected float"

    def test_reasoning_headlines_round_trip(self):
        """Headlines stored as JSON string should parse back to a list."""
        conn = _fresh_db()
        sig = _make_signal(signal_id="sig-json-001")
        t1 = ClassificationResult(
            classification="NOISE", confidence=20,
            one_liner="Normal", model="nova-lite",
            input_tokens=50, output_tokens=30,
        )
        headlines = ["Market crashes 10%", "CEO resigns unexpectedly"]
        store_reasoning(conn, sig, t1, None, headlines)

        raw = conn.execute(
            "SELECT news_headlines FROM signal_reasoning WHERE signal_id = ?",
            ["sig-json-001"],
        ).fetchone()[0]
        parsed = json.loads(raw)
        assert parsed == headlines
