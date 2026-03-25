"""Tests for prompt construction — classifier + reasoner prompts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sentinel.judge.classifier import (
    ClassificationInput,
    ClassificationResult,
    build_input_from_signal,
    build_prompt,
)
from sentinel.judge.reasoner import build_messages
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
def sample_signal():
    return _make_signal()


@pytest.fixture()
def sample_input():
    return ClassificationInput(
        market_question="Will BTC hit $100k by March?",
        category="Crypto",
        side="BUY",
        price=0.72,
        size_usd=5000.0,
        liquidity_usd=50000.0,
        z_score=4.2,
        wallet_win_rate=0.78,
        wallet_total_trades=45,
        funding_age_minutes=8,
        news_headlines="1. Bitcoin surges past $95k\n2. Institutional buying accelerates",
    )


# ── Tier 1 prompt tests ────────────────────────────────────────────────────


class TestBuildPrompt:
    def test_returns_messages_list(self, sample_input):
        messages = build_prompt(sample_input)
        assert isinstance(messages, list)
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        # Nova format: content is a list of {text: ...} dicts
        assert isinstance(messages[0]["content"], list)
        assert "text" in messages[0]["content"][0]

    def test_contains_market_question(self, sample_input):
        messages = build_prompt(sample_input)
        content = messages[0]["content"][0]["text"]
        assert "Will BTC hit $100k by March?" in content

    def test_contains_trade_details(self, sample_input):
        messages = build_prompt(sample_input)
        content = messages[0]["content"][0]["text"]
        assert "BUY" in content
        assert "0.72" in content
        assert "5000.0" in content

    def test_contains_z_score(self, sample_input):
        messages = build_prompt(sample_input)
        content = messages[0]["content"][0]["text"]
        assert "4.2" in content

    def test_contains_win_rate(self, sample_input):
        messages = build_prompt(sample_input)
        content = messages[0]["content"][0]["text"]
        assert "78.0%" in content

    def test_contains_funding_age(self, sample_input):
        messages = build_prompt(sample_input)
        content = messages[0]["content"][0]["text"]
        assert "8" in content

    def test_contains_headlines(self, sample_input):
        messages = build_prompt(sample_input)
        content = messages[0]["content"][0]["text"]
        assert "Bitcoin surges" in content

    def test_contains_json_format_instruction(self, sample_input):
        """JSON format is in the system prompt, not the user message."""
        from sentinel.judge.classifier import _SYSTEM_PROMPT
        assert '"classification"' in _SYSTEM_PROMPT
        assert '"confidence"' in _SYSTEM_PROMPT

    def test_unknown_win_rate(self, sample_input):
        inp = ClassificationInput(
            market_question="Test",
            category="Politics",
            side="SELL",
            price=0.5,
            size_usd=1000.0,
            liquidity_usd=10000.0,
            z_score=3.8,
            wallet_win_rate=None,
            wallet_total_trades=0,
            funding_age_minutes=None,
            news_headlines="No relevant news found.",
        )
        messages = build_prompt(inp)
        content = messages[0]["content"][0]["text"]
        assert "unknown" in content


# ── build_input_from_signal ─────────────────────────────────────────────────


class TestBuildInputFromSignal:
    def test_maps_fields_correctly(self, sample_signal):
        inp = build_input_from_signal(
            sample_signal,
            market_question="Will X happen?",
            category="Politics",
            liquidity_usd=100000.0,
            news_headlines="1. Breaking news",
        )
        assert inp.market_question == "Will X happen?"
        assert inp.category == "Politics"
        assert inp.side == "BUY"
        assert inp.price == 0.72
        assert inp.size_usd == 5000.0
        assert inp.liquidity_usd == 100000.0
        assert inp.z_score == 4.2
        assert inp.wallet_win_rate == 0.78
        assert inp.wallet_total_trades == 45
        assert inp.funding_age_minutes == 8

    def test_none_values_handled(self):
        sig = _make_signal(wallet_win_rate=None, wallet_total_trades=None, funding_age_minutes=None)
        inp = build_input_from_signal(sig)
        assert inp.wallet_win_rate is None
        assert inp.wallet_total_trades == 0
        assert inp.funding_age_minutes is None


# ── Tier 2 prompt tests ────────────────────────────────────────────────────


class TestBuildMessages:
    def test_returns_single_user_message(self, sample_signal):
        t1 = ClassificationResult(
            classification="INFORMED",
            confidence=75,
            one_liner="Large trade before news",
            model="llama3",
            input_tokens=100,
            output_tokens=50,
        )
        messages = build_messages(
            sample_signal,
            news_headlines="1. Breaking news",
            t1_result=t1,
            market_question="Will X?",
            category="Crypto",
        )
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        # Nova format: content is a list of {text: ...} dicts
        assert isinstance(messages[0]["content"], list)
        assert "text" in messages[0]["content"][0]

    def test_contains_market_info(self, sample_signal):
        t1 = ClassificationResult(
            classification="INFORMED", confidence=75,
            one_liner="Test", model="llama3",
            input_tokens=0, output_tokens=0,
        )
        messages = build_messages(
            sample_signal,
            news_headlines="1. News",
            t1_result=t1,
            market_question="Will BTC hit 100k?",
        )
        content = messages[0]["content"][0]["text"]
        assert "Will BTC hit 100k?" in content

    def test_contains_tier1_assessment(self, sample_signal):
        t1 = ClassificationResult(
            classification="INFORMED", confidence=82,
            one_liner="Whale activity detected",
            model="llama3", input_tokens=0, output_tokens=0,
        )
        messages = build_messages(
            sample_signal,
            news_headlines="1. News",
            t1_result=t1,
        )
        content = messages[0]["content"][0]["text"]
        assert "INFORMED" in content
        assert "82" in content
        assert "Whale activity detected" in content

    def test_contains_json_format_instruction(self, sample_signal):
        t1 = ClassificationResult(
            classification="NOISE", confidence=30,
            one_liner="Random", model="llama3",
            input_tokens=0, output_tokens=0,
        )
        messages = build_messages(
            sample_signal,
            news_headlines="1. News",
            t1_result=t1,
        )
        content = messages[0]["content"][0]["text"]
        assert '"suspicion_score"' in content
        assert '"reasoning"' in content
        assert '"key_evidence"' in content

    def test_contains_implied_probability(self, sample_signal):
        t1 = ClassificationResult(
            classification="INFORMED", confidence=60,
            one_liner="Test", model="llama3",
            input_tokens=0, output_tokens=0,
        )
        messages = build_messages(
            sample_signal,
            news_headlines="",
            t1_result=t1,
        )
        content = messages[0]["content"][0]["text"]
        assert "72.0%" in content  # 0.72 * 100
