"""Tests for sentinel.judge.pipeline + store — full Judge orchestration."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import duckdb
import pytest

from sentinel.judge.classifier import ClassificationResult
from sentinel.judge.pipeline import Judge
from sentinel.judge.reasoner import ReasoningResult
from sentinel.judge.store import Alert, build_alert, store_reasoning
from sentinel.scanner.scorer import Signal

# ── Fixtures ────────────────────────────────────────────────────────────────


def _make_signal(**overrides) -> Signal:
    defaults = dict(
        signal_id="sig-001",
        trade_id="trade-001",
        market_id="market-001",
        wallet="0xABCDEF1234567890",
        side="BUY",
        price=0.72,
        size_usd=5000.0,
        trade_timestamp=datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
        volume_z_score=4.2,
        modified_z_score=4.2,
        price_impact=0.015,
        wallet_win_rate=0.78,
        wallet_total_trades=45,
        is_whitelisted=True,
        funding_anomaly=True,
        funding_age_minutes=8,
        statistical_score=75,
        created_at=datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Signal(**defaults)


@pytest.fixture()
def db():
    """In-memory DuckDB with all required tables."""
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
    conn.execute("""
        CREATE TABLE signal_reasoning (
            signal_id       VARCHAR PRIMARY KEY,
            trade_id        VARCHAR NOT NULL,
            classification  VARCHAR,
            tier1_confidence INTEGER,
            suspicion_score INTEGER,
            reasoning       VARCHAR,
            key_evidence    VARCHAR,
            news_headlines  VARCHAR,
            tier1_model     VARCHAR,
            tier2_model     VARCHAR,
            tier1_tokens    INTEGER,
            tier2_tokens    INTEGER,
            tier2_used      BOOLEAN DEFAULT FALSE,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE markets (
            market_id    VARCHAR PRIMARY KEY,
            question     VARCHAR,
            slug         VARCHAR,
            category     VARCHAR,
            end_date     TIMESTAMP,
            volume_usd   DECIMAL(18,6),
            liquidity_usd DECIMAL(18,6),
            active       BOOLEAN DEFAULT TRUE,
            resolved     BOOLEAN DEFAULT FALSE,
            resolved_price DECIMAL(10,6),
            last_synced  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Insert a sample market for lookup
    conn.execute(
        "INSERT INTO markets (market_id, question, category, liquidity_usd) VALUES (?, ?, ?, ?)",
        ["market-001", "Will BTC hit $100k?", "Crypto", 50000.0],
    )
    yield conn
    conn.close()


# ── store_reasoning ─────────────────────────────────────────────────────────


class TestStoreReasoning:
    def test_writes_tier1_only(self, db):
        signal = _make_signal()
        t1 = ClassificationResult(
            classification="INFORMED", confidence=70,
            one_liner="Suspicious", model="llama3",
            input_tokens=100, output_tokens=50,
        )
        store_reasoning(db, signal, t1, None, ["Headline 1"])
        row = db.execute(
            "SELECT classification, tier1_confidence, suspicion_score, tier2_model FROM signal_reasoning WHERE signal_id = ?",
            ["sig-001"],
        ).fetchone()
        assert row is not None
        assert row[0] == "INFORMED"
        assert row[1] == 70
        assert row[2] == 70  # No T2, so suspicion = T1 confidence
        assert row[3] is None

    def test_writes_tier1_and_tier2(self, db):
        signal = _make_signal()
        t1 = ClassificationResult(
            classification="INFORMED", confidence=75,
            one_liner="Suspicious", model="llama3",
            input_tokens=100, output_tokens=50,
        )
        t2 = ReasoningResult(
            suspicion_score=88, reasoning="Very suspicious.",
            key_evidence="Timing", model="claude",
            input_tokens=200, output_tokens=80,
        )
        store_reasoning(db, signal, t1, t2, ["Headline 1", "Headline 2"])
        row = db.execute(
            "SELECT suspicion_score, reasoning, tier2_model, news_headlines FROM signal_reasoning WHERE signal_id = ?",
            ["sig-001"],
        ).fetchone()
        assert row[0] == 88
        assert row[1] == "Very suspicious."
        assert row[2] == "claude"
        headlines = json.loads(row[3])
        assert len(headlines) == 2

    def test_upsert_replaces(self, db):
        signal = _make_signal()
        t1 = ClassificationResult(
            classification="NOISE", confidence=20,
            one_liner="Random", model="llama3",
            input_tokens=50, output_tokens=30,
        )
        store_reasoning(db, signal, t1, None, [])
        # Store again with different data
        t1b = ClassificationResult(
            classification="INFORMED", confidence=90,
            one_liner="Updated", model="llama3",
            input_tokens=50, output_tokens=30,
        )
        store_reasoning(db, signal, t1b, None, ["New headline"])
        count = db.execute("SELECT COUNT(*) FROM signal_reasoning WHERE signal_id = ?", ["sig-001"]).fetchone()[0]
        assert count == 1
        row = db.execute("SELECT classification FROM signal_reasoning WHERE signal_id = ?", ["sig-001"]).fetchone()
        assert row[0] == "INFORMED"


# ── build_alert ─────────────────────────────────────────────────────────────


class TestBuildAlert:
    def test_alert_emitted_above_threshold(self):
        signal = _make_signal()
        t1 = ClassificationResult(
            classification="INFORMED", confidence=85,
            one_liner="Suspicious", model="llama3",
            input_tokens=0, output_tokens=0,
        )
        alert = build_alert(signal, t1, None)
        assert alert is not None
        assert alert.score == 85
        assert alert.classification == "INFORMED"

    def test_no_alert_below_threshold(self):
        signal = _make_signal()
        t1 = ClassificationResult(
            classification="NOISE", confidence=40,
            one_liner="Normal", model="llama3",
            input_tokens=0, output_tokens=0,
        )
        alert = build_alert(signal, t1, None)
        assert alert is None

    def test_tier2_score_used_when_present(self):
        signal = _make_signal()
        t1 = ClassificationResult(
            classification="INFORMED", confidence=70,
            one_liner="Suspicious", model="llama3",
            input_tokens=0, output_tokens=0,
        )
        t2 = ReasoningResult(
            suspicion_score=92, reasoning="High suspicion.",
            key_evidence="Evidence", model="claude",
            input_tokens=0, output_tokens=0,
        )
        alert = build_alert(signal, t1, t2)
        assert alert is not None
        assert alert.score == 92

    def test_tier2_below_threshold_no_alert(self):
        signal = _make_signal()
        t1 = ClassificationResult(
            classification="INFORMED", confidence=85,
            one_liner="Suspicious", model="llama3",
            input_tokens=0, output_tokens=0,
        )
        t2 = ReasoningResult(
            suspicion_score=50, reasoning="On review, not suspicious.",
            key_evidence="None", model="claude",
            input_tokens=0, output_tokens=0,
        )
        alert = build_alert(signal, t1, t2)
        assert alert is None  # T2=50 < 80 threshold


# ── Judge pipeline (dry run) ───────────────────────────────────────────────


class TestJudgePipeline:
    async def test_dry_run_no_bedrock(self, db):
        """In dry-run mode, Judge should skip Bedrock calls."""
        judge_queue: asyncio.Queue[Signal] = asyncio.Queue()
        alert_queue: asyncio.Queue[Alert] = asyncio.Queue()

        signal = _make_signal()
        await judge_queue.put(signal)

        judge = Judge(
            db, judge_queue=judge_queue, alert_queue=alert_queue, dry_run=True, max_workers=1
        )

        async def _stop_after_drain():
            await judge_queue.join()
            judge.stop()

        stop_task = asyncio.create_task(_stop_after_drain())

        with patch("sentinel.judge.pipeline.fetch_news", new_callable=AsyncMock, return_value=[]):
            await asyncio.gather(stop_task, judge.run())

        assert judge.signals_processed == 1
        assert judge.tier1_calls == 0  # No Bedrock in dry run

    @patch("sentinel.judge.budget._TIER_LIMITS", {"tier1": 200, "tier2": 30})
    async def test_budget_exhausted_skips(self, db):
        """When Tier 1 budget is exhausted, signals are skipped."""
        # Exhaust tier1 budget
        today = datetime.now(tz=UTC).date()
        db.execute("INSERT INTO llm_budget VALUES (?, 'tier1', 200, 200)", [today])

        judge_queue: asyncio.Queue[Signal] = asyncio.Queue()
        alert_queue: asyncio.Queue[Alert] = asyncio.Queue()
        signal = _make_signal()
        await judge_queue.put(signal)

        judge = Judge(db, judge_queue=judge_queue, alert_queue=alert_queue, max_workers=1)

        async def _stop_after_drain():
            await judge_queue.join()
            judge.stop()

        stop_task = asyncio.create_task(_stop_after_drain())

        with patch("sentinel.judge.pipeline.fetch_news", new_callable=AsyncMock, return_value=[]):
            await asyncio.gather(stop_task, judge.run())

        assert judge.skipped_budget == 1
        assert judge.tier1_calls == 0

    @patch("sentinel.judge.budget._TIER_LIMITS", {"tier1": 200, "tier2": 30})
    async def test_full_flow_mocked(self, db):
        """Full flow with mocked Bedrock: classify → reason → store → alert."""
        judge_queue: asyncio.Queue[Signal] = asyncio.Queue()
        alert_queue: asyncio.Queue[Alert] = asyncio.Queue()

        signal = _make_signal()
        await judge_queue.put(signal)

        judge = Judge(db, judge_queue=judge_queue, alert_queue=alert_queue)

        t1_result = ClassificationResult(
            classification="INFORMED", confidence=80,
            one_liner="Suspicious timing", model="llama3",
            input_tokens=100, output_tokens=50
        )
        t2_result = ReasoningResult(
            suspicion_score=90, reasoning="High suspicion.",
            key_evidence="Timing", model="claude",
            input_tokens=200, output_tokens=80,
        )

        async def _stop_after_drain():
            await judge_queue.join()
            judge.stop()

        stop_task = asyncio.create_task(_stop_after_drain())

        with (
            patch("sentinel.judge.pipeline.fetch_news", new_callable=AsyncMock, return_value=["News 1"]),
            patch("sentinel.judge.pipeline.classify", new_callable=AsyncMock, return_value=t1_result),
            patch("sentinel.judge.pipeline.reason", new_callable=AsyncMock, return_value=t2_result),
        ):
            await asyncio.gather(stop_task, judge.run())

        assert judge.signals_processed == 1
        assert judge.tier1_calls == 1
        assert judge.tier2_calls == 1
        assert judge.alerts_emitted == 1

        # Check DuckDB
        row = db.execute("SELECT suspicion_score FROM signal_reasoning WHERE signal_id = ?", ["sig-001"]).fetchone()
        assert row is not None
        assert row[0] == 90

        # Check alert queue
        alert = alert_queue.get_nowait()
        assert alert.score == 90

    async def test_tier2_skipped_low_confidence(self, db):
        """When T1 confidence < threshold, Tier 2 is skipped."""
        judge_queue: asyncio.Queue[Signal] = asyncio.Queue()
        alert_queue: asyncio.Queue[Alert] = asyncio.Queue()

        signal = _make_signal()
        await judge_queue.put(signal)

        judge = Judge(db, judge_queue=judge_queue, alert_queue=alert_queue)

        t1_result = ClassificationResult(
            classification="NOISE", confidence=30,
            one_liner="Normal", model="llama3",
            input_tokens=100, output_tokens=50
        )

        async def _stop_after_drain():
            await judge_queue.join()
            judge.stop()

        stop_task = asyncio.create_task(_stop_after_drain())

        with (
            patch("sentinel.judge.pipeline.fetch_news", new_callable=AsyncMock, return_value=[]),
            patch("sentinel.judge.pipeline.classify", new_callable=AsyncMock, return_value=t1_result),
            patch("sentinel.judge.pipeline.reason", new_callable=AsyncMock) as mock_reason,
        ):
            await asyncio.gather(stop_task, judge.run())

        mock_reason.assert_not_called()
        assert judge.tier2_calls == 0
        assert judge.alerts_emitted == 0  # 30 < 80

    async def test_market_lookup(self, db):
        """Judge should look up market question from DuckDB."""
        judge_queue: asyncio.Queue[Signal] = asyncio.Queue()
        alert_queue: asyncio.Queue[Alert] = asyncio.Queue()

        signal = _make_signal()
        await judge_queue.put(signal)

        judge = Judge(db, judge_queue=judge_queue, alert_queue=alert_queue, max_workers=1)

        captured_kwargs = {}

        async def _capture_classify(sig, **kwargs):
            captured_kwargs.update(kwargs)
            return ClassificationResult(
                classification="NOISE", confidence=20,
                one_liner="Normal", model="llama3",
                input_tokens=50, output_tokens=30,
            )

        async def _stop_after_drain():
            await judge_queue.join()
            judge.stop()

        stop_task = asyncio.create_task(_stop_after_drain())

        with (
            patch("sentinel.judge.pipeline.fetch_news", new_callable=AsyncMock, return_value=[]),
            patch("sentinel.judge.pipeline.classify", side_effect=_capture_classify),
        ):
            await asyncio.gather(stop_task, judge.run())

        assert captured_kwargs.get("market_question") == "Will BTC hit $100k?"
        assert captured_kwargs.get("category") == "Crypto"
