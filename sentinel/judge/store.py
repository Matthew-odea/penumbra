"""Judge result persistence — DuckDB + alert emission.

Writes every classification to the ``signal_reasoning`` table and pushes
high-scoring signals (≥ ``settings.alert_min_score``) onto the alert queue
for downstream delivery (Sprint 4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import structlog

from sentinel.config import settings
from sentinel.judge.classifier import ClassificationResult
from sentinel.judge.reasoner import ReasoningResult
from sentinel.scanner.scorer import Signal

logger = structlog.get_logger()

# ── Alert data class ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Alert:
    """A high-suspicion signal ready for delivery (Sprint 4 consumes these)."""

    signal: Signal
    score: int
    reasoning: str
    key_evidence: str = ""
    classification: str = "INFORMED"


# ── DuckDB persistence ─────────────────────────────────────────────────────

_INSERT_REASONING_SQL = """
INSERT OR REPLACE INTO signal_reasoning (
    signal_id, trade_id, classification, tier1_confidence,
    suspicion_score, reasoning, key_evidence, news_headlines,
    tier1_model, tier2_model, tier1_tokens, tier2_tokens, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
"""


def store_reasoning(
    db: Any,
    signal: Signal,
    t1_result: ClassificationResult,
    t2_result: ReasoningResult | None,
    headlines: list[str],
) -> None:
    """Persist a judge result to the ``signal_reasoning`` table."""
    final_score = t2_result.suspicion_score if t2_result else t1_result.confidence
    reasoning = t2_result.reasoning if t2_result else t1_result.one_liner
    key_evidence = t2_result.key_evidence if t2_result else ""

    db.execute(
        _INSERT_REASONING_SQL,
        [
            signal.signal_id,
            signal.trade_id,
            t1_result.classification,
            t1_result.confidence,
            final_score,
            reasoning,
            key_evidence,
            json.dumps(headlines),
            t1_result.model,
            t2_result.model if t2_result else None,
            t1_result.input_tokens + t1_result.output_tokens,
            (t2_result.input_tokens + t2_result.output_tokens) if t2_result else None,
        ],
    )
    logger.info(
        "Reasoning stored",
        signal_id=signal.signal_id,
        classification=t1_result.classification,
        suspicion_score=final_score,
    )


def build_alert(
    signal: Signal,
    t1_result: ClassificationResult,
    t2_result: ReasoningResult | None,
) -> Alert | None:
    """Create an ``Alert`` if the final score meets the threshold."""
    final_score = t2_result.suspicion_score if t2_result else t1_result.confidence
    if final_score < settings.alert_min_score:
        return None

    reasoning = t2_result.reasoning if t2_result else t1_result.one_liner
    key_evidence = t2_result.key_evidence if t2_result else ""

    return Alert(
        signal=signal,
        score=final_score,
        reasoning=reasoning,
        key_evidence=key_evidence,
        classification=t1_result.classification,
    )
