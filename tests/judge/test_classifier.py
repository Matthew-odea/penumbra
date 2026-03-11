"""Tests for sentinel.judge.classifier — Tier 1 Amazon Nova Lite classifier."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from sentinel.judge.classifier import (
    ClassificationResult,
    classify,
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


def _mock_bedrock_response(generation: str, *, input_tokens: int = 100, output_tokens: int = 50):
    """Create a mock boto3 response matching Amazon Nova format."""
    body = json.dumps({
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": generation}],
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


# ── classify (mocked Bedrock) ──────────────────────────────────────────────


class TestClassify:
    async def test_informed_classification(self):
        generation = json.dumps({
            "classification": "INFORMED",
            "confidence": 80,
            "one_liner": "Large bet before breaking news",
        })
        client = _mock_bedrock_response(generation)
        signal = _make_signal()

        result = await classify(
            signal,
            news_headlines="1. News",
            market_question="Will X happen?",
            bedrock_client=client,
        )
        assert result.classification == "INFORMED"
        assert result.confidence == 80
        assert result.input_tokens == 100
        assert result.output_tokens == 50

    async def test_noise_classification(self):
        generation = json.dumps({
            "classification": "NOISE",
            "confidence": 25,
            "one_liner": "Normal retail activity",
        })
        client = _mock_bedrock_response(generation)
        signal = _make_signal()

        result = await classify(signal, bedrock_client=client)
        assert result.classification == "NOISE"
        assert result.confidence == 25

    async def test_malformed_response_returns_noise(self):
        client = _mock_bedrock_response("I think this trade looks suspicious but...")
        signal = _make_signal()

        result = await classify(signal, bedrock_client=client)
        assert result.classification == "NOISE"
        assert result.confidence == 0

    async def test_bedrock_error_returns_noise(self):
        client = MagicMock()
        client.invoke_model.side_effect = Exception("Throttled")
        signal = _make_signal()

        result = await classify(signal, bedrock_client=client)
        assert result.classification == "NOISE"
        assert result.confidence == 0
        assert "Bedrock error" in result.one_liner

    async def test_model_id_in_result(self):
        generation = json.dumps({"classification": "NOISE", "confidence": 10, "one_liner": "X"})
        client = _mock_bedrock_response(generation)
        signal = _make_signal()

        result = await classify(signal, bedrock_client=client)
        assert result.model == "amazon.nova-lite-v1:0"

    async def test_json_with_extra_text(self):
        generation = (
            'Here is my analysis:\n'
            '{"classification": "INFORMED", "confidence": 72, "one_liner": "Timing is suspicious"}\n'
            'I hope this helps.'
        )
        client = _mock_bedrock_response(generation)
        signal = _make_signal()

        result = await classify(signal, bedrock_client=client)
        assert result.classification == "INFORMED"
        assert result.confidence == 72
