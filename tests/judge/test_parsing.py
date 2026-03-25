"""Tests for response parsing — classifier + reasoner."""

from __future__ import annotations

import json

from sentinel.judge.classifier import parse_classification
from sentinel.judge.reasoner import parse_reasoning

# ── Tier 1 parsing ──────────────────────────────────────────────────────────


class TestParseClassification:
    def test_valid_json(self):
        raw = json.dumps({
            "classification": "INFORMED",
            "confidence": 85,
            "one_liner": "Large trade before breaking news",
        })
        result = parse_classification(raw, model="llama3")
        assert result.classification == "INFORMED"
        assert result.confidence == 85
        assert result.one_liner == "Large trade before breaking news"
        assert result.model == "llama3"

    def test_noise_classification(self):
        raw = json.dumps({
            "classification": "NOISE",
            "confidence": 20,
            "one_liner": "Normal retail activity",
        })
        result = parse_classification(raw, model="llama3")
        assert result.classification == "NOISE"
        assert result.confidence == 20

    def test_json_with_surrounding_text(self):
        raw = 'Here is my analysis:\n{"classification": "INFORMED", "confidence": 70, "one_liner": "Suspicious timing"}\nEnd.'
        result = parse_classification(raw, model="llama3")
        assert result.classification == "INFORMED"
        assert result.confidence == 70

    def test_json_with_leading_whitespace(self):
        raw = '  \n  {"classification": "NOISE", "confidence": 10, "one_liner": "Normal"}'
        result = parse_classification(raw, model="llama3")
        assert result.classification == "NOISE"
        assert result.confidence == 10

    def test_confidence_clamped_high(self):
        raw = json.dumps({"classification": "INFORMED", "confidence": 150, "one_liner": "X"})
        result = parse_classification(raw)
        assert result.confidence == 100

    def test_confidence_clamped_low(self):
        raw = json.dumps({"classification": "INFORMED", "confidence": -10, "one_liner": "X"})
        result = parse_classification(raw)
        assert result.confidence == 0

    def test_invalid_classification_defaults_noise(self):
        raw = json.dumps({"classification": "MAYBE", "confidence": 50, "one_liner": "Unsure"})
        result = parse_classification(raw)
        assert result.classification == "NOISE"

    def test_lowercase_classification_normalized(self):
        raw = json.dumps({"classification": "informed", "confidence": 60, "one_liner": "X"})
        result = parse_classification(raw)
        assert result.classification == "INFORMED"

    def test_completely_malformed(self):
        raw = "This is not JSON at all, just rambling text about markets."
        result = parse_classification(raw, model="llama3")
        assert result.classification == "NOISE"
        assert result.confidence == 0
        assert "Unable to parse" in result.one_liner

    def test_empty_string(self):
        result = parse_classification("", model="llama3")
        assert result.classification == "NOISE"
        assert result.confidence == 0

    def test_missing_fields_have_defaults(self):
        raw = json.dumps({"classification": "INFORMED"})
        result = parse_classification(raw)
        assert result.confidence == 0
        assert result.one_liner == ""


# ── Tier 2 parsing ──────────────────────────────────────────────────────────


class TestParseReasoning:
    def test_valid_json(self):
        raw = json.dumps({
            "suspicion_score": 88,
            "reasoning": "Trade placed 12 minutes before press release. Wallet has 85% win rate.",
            "key_evidence": "Trade timing precedes news by 12 minutes",
        })
        result = parse_reasoning(raw, model="claude")
        assert result.suspicion_score == 88
        assert "12 minutes" in result.reasoning
        assert "timing" in result.key_evidence
        assert result.model == "claude"

    def test_json_with_surrounding_text(self):
        raw = 'Let me analyze this:\n{"suspicion_score": 45, "reasoning": "Normal activity.", "key_evidence": "No unusual patterns"}\nDone.'
        result = parse_reasoning(raw, model="claude")
        assert result.suspicion_score == 45

    def test_score_clamped_high(self):
        raw = json.dumps({"suspicion_score": 200, "reasoning": "X", "key_evidence": "Y"})
        result = parse_reasoning(raw)
        assert result.suspicion_score == 100

    def test_score_clamped_low(self):
        raw = json.dumps({"suspicion_score": -5, "reasoning": "X", "key_evidence": "Y"})
        result = parse_reasoning(raw)
        assert result.suspicion_score == 0

    def test_completely_malformed(self):
        raw = "I cannot provide a JSON response for this analysis."
        result = parse_reasoning(raw, model="claude")
        assert result.suspicion_score == 0
        assert "Unable to parse" in result.reasoning

    def test_empty_string(self):
        result = parse_reasoning("", model="claude")
        assert result.suspicion_score == 0

    def test_missing_fields_have_defaults(self):
        raw = json.dumps({"suspicion_score": 60})
        result = parse_reasoning(raw)
        assert result.reasoning == ""
        assert result.key_evidence == ""

    def test_leading_whitespace(self):
        raw = '  \n\n{"suspicion_score": 75, "reasoning": "Test.", "key_evidence": "Evidence."}'
        result = parse_reasoning(raw)
        assert result.suspicion_score == 75
