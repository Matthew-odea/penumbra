"""Tests for sentinel.judge.reasoner — Tier 2 Amazon Nova Pro reasoner."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from sentinel.judge.classifier import ClassificationResult
from sentinel.judge.reasoner import (
    ReasoningResult,
    reason,
)
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


def _make_t1_result(**overrides) -> ClassificationResult:
    defaults = dict(
        classification="INFORMED",
        confidence=75,
        one_liner="Suspicious timing",
        model="llama3",
        input_tokens=100,
        output_tokens=50,
    )
    defaults.update(overrides)
    return ClassificationResult(**defaults)


def _mock_bedrock_claude(generation_text: str, *, input_tokens: int = 200, output_tokens: int = 80):
    """Create a mock boto3 response matching Amazon Nova format."""
    body = json.dumps({
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": generation_text}],
            }
        },
        "stopReason": "end_turn",
        "usage": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
        },
    }).encode()
    mock_resp = {"body": io.BytesIO(body)}
    client = MagicMock()
    client.invoke_model.return_value = mock_resp
    return client


# ── reason (mocked Bedrock) ────────────────────────────────────────────────


class TestReason:
    async def test_high_suspicion_result(self):
        generation = json.dumps({
            "suspicion_score": 88,
            "reasoning": "Trade placed 12 minutes before press release. Wallet has 85% win rate.",
            "key_evidence": "Trade timing precedes news by 12 minutes",
        })
        client = _mock_bedrock_claude(generation)
        signal = _make_signal()
        t1 = _make_t1_result()

        result = await reason(
            signal,
            news_headlines="1. Breaking news",
            t1_result=t1,
            market_question="Will X?",
            bedrock_client=client,
        )
        assert result.suspicion_score == 88
        assert "12 minutes" in result.reasoning
        assert result.input_tokens == 200
        assert result.output_tokens == 80

    async def test_low_suspicion_result(self):
        generation = json.dumps({
            "suspicion_score": 15,
            "reasoning": "Normal trading activity with no unusual patterns.",
            "key_evidence": "Trade size is within normal range",
        })
        client = _mock_bedrock_claude(generation)
        signal = _make_signal()
        t1 = _make_t1_result(confidence=65)

        result = await reason(
            signal,
            news_headlines="No relevant news found.",
            t1_result=t1,
            bedrock_client=client,
        )
        assert result.suspicion_score == 15

    async def test_malformed_response_fallback(self):
        client = _mock_bedrock_claude("This trade is definitely suspicious but I can't format JSON.")
        signal = _make_signal()
        t1 = _make_t1_result()

        result = await reason(
            signal,
            news_headlines="1. News",
            t1_result=t1,
            bedrock_client=client,
        )
        assert result.suspicion_score == 0
        assert "Unable to parse" in result.reasoning

    async def test_bedrock_timeout_uses_tier1_fallback(self):
        client = MagicMock()
        client.invoke_model.side_effect = Exception("ModelTimeoutException")
        signal = _make_signal()
        t1 = _make_t1_result(confidence=72)

        result = await reason(
            signal,
            news_headlines="1. News",
            t1_result=t1,
            bedrock_client=client,
        )
        # Falls back to T1 confidence
        assert result.suspicion_score == 72
        assert "Tier 2 failed" in result.reasoning

    async def test_model_id_in_result(self):
        generation = json.dumps({
            "suspicion_score": 50,
            "reasoning": "Mid-range.",
            "key_evidence": "Mixed signals",
        })
        client = _mock_bedrock_claude(generation)
        signal = _make_signal()
        t1 = _make_t1_result()

        result = await reason(
            signal,
            news_headlines="",
            t1_result=t1,
            bedrock_client=client,
        )
        assert result.model == "amazon.nova-pro-v1:0"

    async def test_json_surrounded_by_text(self):
        generation = (
            "After careful analysis:\n"
            '{"suspicion_score": 65, "reasoning": "Moderate concern.", "key_evidence": "Funding timing"}\n'
            "That's my assessment."
        )
        client = _mock_bedrock_claude(generation)
        signal = _make_signal()
        t1 = _make_t1_result()

        result = await reason(
            signal,
            news_headlines="1. News",
            t1_result=t1,
            bedrock_client=client,
        )
        assert result.suspicion_score == 65
