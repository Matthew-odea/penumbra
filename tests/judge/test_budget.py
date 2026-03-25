"""Tests for sentinel.judge.budget — BudgetManager."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

import duckdb
import pytest

from sentinel.judge.budget import BudgetManager, BudgetStatus

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def db():
    """In-memory DuckDB with llm_budget table."""
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE llm_budget (
            date   DATE NOT NULL,
            tier   VARCHAR NOT NULL,
            calls_used  INTEGER DEFAULT 0,
            calls_limit INTEGER NOT NULL,
            PRIMARY KEY (date, tier)
        )
    """)
    yield conn
    conn.close()


@pytest.fixture()
def budget(db):
    return BudgetManager(db)


# ── BudgetStatus ────────────────────────────────────────────────────────────


class TestBudgetStatus:
    def test_remaining_positive(self):
        s = BudgetStatus(tier="tier1", calls_used=50, calls_limit=200)
        assert s.remaining == 150

    def test_remaining_zero(self):
        s = BudgetStatus(tier="tier1", calls_used=200, calls_limit=200)
        assert s.remaining == 0

    def test_remaining_negative_clamped(self):
        s = BudgetStatus(tier="tier1", calls_used=210, calls_limit=200)
        assert s.remaining == 0

    def test_not_exhausted(self):
        s = BudgetStatus(tier="tier1", calls_used=199, calls_limit=200)
        assert not s.is_exhausted

    def test_exhausted_at_limit(self):
        s = BudgetStatus(tier="tier1", calls_used=200, calls_limit=200)
        assert s.is_exhausted

    def test_exhausted_over_limit(self):
        s = BudgetStatus(tier="tier1", calls_used=201, calls_limit=200)
        assert s.is_exhausted


# ── BudgetManager.can_call ──────────────────────────────────────────────────


class TestCanCall:
    @patch("sentinel.judge.budget._TIER_LIMITS", {"tier1": 200, "tier2": 30})
    def test_can_call_when_empty(self, budget):
        assert budget.can_call("tier1") is True
        assert budget.can_call("tier2") is True

    def test_can_call_after_recording(self, budget):
        budget.record_call("tier1")
        assert budget.can_call("tier1") is True  # 1 < 200

    def test_cannot_call_when_exhausted(self, budget, db):
        today = datetime.now(tz=UTC).date()
        db.execute(
            "INSERT INTO llm_budget VALUES (?, 'tier2', 30, 30)",
            [today],
        )
        assert budget.can_call("tier2") is False


# ── BudgetManager.record_call ──────────────────────────────────────────────


class TestRecordCall:
    def test_first_call_creates_row(self, budget, db):
        budget.record_call("tier1")
        today = datetime.now(tz=UTC).date()
        row = db.execute(
            "SELECT calls_used FROM llm_budget WHERE date = ? AND tier = 'tier1'",
            [today],
        ).fetchone()
        assert row is not None
        assert row[0] == 1

    def test_subsequent_calls_increment(self, budget, db):
        budget.record_call("tier1")
        budget.record_call("tier1")
        budget.record_call("tier1")
        today = datetime.now(tz=UTC).date()
        row = db.execute(
            "SELECT calls_used FROM llm_budget WHERE date = ? AND tier = 'tier1'",
            [today],
        ).fetchone()
        assert row[0] == 3

    def test_different_tiers_tracked_separately(self, budget, db):
        budget.record_call("tier1")
        budget.record_call("tier2")
        budget.record_call("tier1")
        today = datetime.now(tz=UTC).date()
        t1 = db.execute(
            "SELECT calls_used FROM llm_budget WHERE date = ? AND tier = 'tier1'",
            [today],
        ).fetchone()
        t2 = db.execute(
            "SELECT calls_used FROM llm_budget WHERE date = ? AND tier = 'tier2'",
            [today],
        ).fetchone()
        assert t1[0] == 2
        assert t2[0] == 1


# ── BudgetManager.get_status ───────────────────────────────────────────────


class TestGetStatus:
    def test_returns_both_tiers(self, budget):
        status = budget.get_status()
        assert "tier1" in status
        assert "tier2" in status

    def test_reflects_recorded_calls(self, budget):
        budget.record_call("tier1")
        budget.record_call("tier1")
        status = budget.get_status()
        assert status["tier1"].calls_used == 2
        assert status["tier2"].calls_used == 0

    @patch("sentinel.judge.budget._TIER_LIMITS", {"tier1": 200, "tier2": 30})
    def test_limits_match_config(self, budget):
        status = budget.get_status()
        assert status["tier1"].calls_limit == 200
        assert status["tier2"].calls_limit == 30


# ── Midnight reset ──────────────────────────────────────────────────────────


class TestMidnightReset:
    def test_yesterday_calls_not_counted(self, budget, db):
        """Calls from a previous day should not affect today's budget."""
        yesterday = date(2025, 1, 1)
        db.execute(
            "INSERT INTO llm_budget VALUES (?, 'tier1', 200, 200)",
            [yesterday],
        )
        # Today is a new day — should have full budget
        assert budget.can_call("tier1") is True


# ── 201st call rejected ────────────────────────────────────────────────────


class TestBudgetExhaustion:
    @patch("sentinel.judge.budget._TIER_LIMITS", {"tier1": 200, "tier2": 30})
    def test_201st_tier1_rejected(self, budget, db):
        """After 200 Tier 1 calls, the 201st should be rejected."""
        today = datetime.now(tz=UTC).date()
        db.execute(
            "INSERT INTO llm_budget VALUES (?, 'tier1', 200, 200)",
            [today],
        )
        assert budget.can_call("tier1") is False

    def test_31st_tier2_rejected(self, budget, db):
        """After 30 Tier 2 calls, the 31st should be rejected."""
        today = datetime.now(tz=UTC).date()
        db.execute(
            "INSERT INTO llm_budget VALUES (?, 'tier2', 30, 30)",
            [today],
        )
        assert budget.can_call("tier2") is False
