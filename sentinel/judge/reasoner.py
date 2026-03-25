"""Tier 2 — Amazon Nova Pro reasoner via AWS Bedrock.

Produces a detailed suspicion score, 2-sentence reasoning, and a key
evidence highlight for signals that pass the Tier 1 confidence gate
(≥ ``settings.bedrock_tier2_min_suspicion``).
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import structlog

from sentinel.config import settings
from sentinel.judge.classifier import ClassificationResult
from sentinel.scanner.scorer import Signal

logger = structlog.get_logger()

# ── Data classes ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReasoningResult:
    """Parsed output from Amazon Nova Pro."""

    suspicion_score: int  # 1-100
    reasoning: str  # 2-sentence explanation
    key_evidence: str  # Single most important factor
    model: str
    input_tokens: int
    output_tokens: int
    is_fallback: bool = False


# ── Prompt construction ─────────────────────────────────────────────────────

_USER_TEMPLATE = """\
You are a senior prediction market intelligence analyst.

A trade has been flagged by our statistical system:

MARKET: "{market_question}"
CATEGORY: {category}
TRADE: {side} ${size_usd} at {price} (implied prob: {implied_prob}%)
WALLET: Win rate {win_rate} across {total_trades} resolved markets
STATISTICAL ANOMALY: Volume Z-score {z_score}, funded {funding_age_minutes}min before trade

NEWS CONTEXT (last 24h):
{news_headlines}

TIER 1 ASSESSMENT: {t1_classification} (confidence {t1_confidence}/100)
Tier 1 reasoning: {t1_one_liner}

ANALYSIS REQUIRED:
1. Is this trade's timing suspiciously early relative to the news?
2. Could the trader have information not yet reflected in headlines?
3. Rate suspicion 1-100 (100 = almost certainly informed)

Respond as JSON:
{{"suspicion_score": N, "reasoning": "Two sentences explaining your assessment.", \
"key_evidence": "The single most important factor."}}"""


def build_messages(
    signal: Signal,
    *,
    news_headlines: str,
    t1_result: ClassificationResult,
    market_question: str = "",
    category: str = "",
) -> list[dict[str, str]]:
    """Build the Amazon Nova Messages payload."""
    content = _USER_TEMPLATE.format(
        market_question=market_question,
        category=category,
        side=signal.side,
        size_usd=signal.size_usd,
        price=signal.price,
        implied_prob=round(signal.price * 100, 1),
        win_rate=f"{signal.wallet_win_rate:.1%}" if signal.wallet_win_rate else "unknown",
        total_trades=signal.wallet_total_trades or 0,
        z_score=round(signal.modified_z_score, 2),
        funding_age_minutes=signal.funding_age_minutes if signal.funding_age_minutes else "N/A",
        news_headlines=news_headlines,
        t1_classification=t1_result.classification,
        t1_confidence=t1_result.confidence,
        t1_one_liner=t1_result.one_liner,
    )
    return [{"role": "user", "content": [{"text": content}]}]


# ── Response parsing ────────────────────────────────────────────────────────

_JSON_PATTERN = re.compile(r"\{[^{}]*\}")


def parse_reasoning(raw: str, *, model: str = "") -> ReasoningResult:
    """Extract a ``ReasoningResult`` from Nova's raw text response.

    Tries strict ``json.loads`` first, then regex extraction, then falls
    back to a conservative low-suspicion result.
    """
    # 1. Try direct JSON parse
    try:
        obj = json.loads(raw.strip())
        return _result_from_dict(obj, model=model)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass

    # 2. Try regex extraction
    match = _JSON_PATTERN.search(raw)
    if match:
        try:
            obj = json.loads(match.group())
            return _result_from_dict(obj, model=model)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

    # 3. Fallback
    logger.warning("Failed to parse Tier 2 response, using fallback", raw=raw[:200])
    return ReasoningResult(
        suspicion_score=0,
        reasoning="Unable to parse model response",
        key_evidence="N/A",
        model=model,
        input_tokens=0,
        output_tokens=0,
    )


def _result_from_dict(obj: dict[str, Any], *, model: str) -> ReasoningResult:
    score = int(obj.get("suspicion_score", 0))
    score = max(0, min(100, score))
    reasoning = str(obj.get("reasoning", ""))
    key_evidence = str(obj.get("key_evidence", ""))
    return ReasoningResult(
        suspicion_score=score,
        reasoning=reasoning,
        key_evidence=key_evidence,
        model=model,
        input_tokens=0,
        output_tokens=0,
    )


# ── Bedrock invocation ─────────────────────────────────────────────────────


_bedrock_client: Any = None
_bedrock_client_lock = threading.Lock()


def _get_bedrock_client() -> Any:
    """Return a module-level singleton boto3 bedrock-runtime client.

    Credentials are resolved via the boto3 default chain (env vars →
    IAM instance profile → ~/.aws/credentials).
    """
    global _bedrock_client
    if _bedrock_client is None:
        with _bedrock_client_lock:
            if _bedrock_client is None:
                import boto3
                _bedrock_client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
    return _bedrock_client


async def reason(
    signal: Signal,
    *,
    news_headlines: str,
    t1_result: ClassificationResult,
    market_question: str = "",
    category: str = "",
    bedrock_client: Any | None = None,
    timeout_s: float = 5.0,
) -> ReasoningResult:
    """Run Tier 2 reasoning on a signal that passed the Tier 1 gate.

    Args:
        signal: The flagged signal.
        news_headlines: Pre-formatted headline string.
        t1_result: The Tier 1 classification result.
        market_question: Market question text.
        category: Market category.
        bedrock_client: Optional pre-built boto3 client (for testing).
        timeout_s: Bedrock timeout in seconds (default 5).

    Returns:
        ReasoningResult with Nova Pro's assessment.
    """
    messages = build_messages(
        signal,
        news_headlines=news_headlines,
        t1_result=t1_result,
        market_question=market_question,
        category=category,
    )
    model_id = settings.bedrock_tier2_model
    client = bedrock_client or _get_bedrock_client()

    t0 = time.monotonic()
    request_body = json.dumps({
        "schemaVersion": "messages-v1",
        "messages": messages,
        "inferenceConfig": {
            "maxTokens": 512,
            "temperature": 0.2,
        },
    })
    try:
        import asyncio
        response = await asyncio.to_thread(
            client.invoke_model,
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=request_body,
        )
        body = json.loads(response["body"].read())

        # Nova format: output.message.content[0].text
        output_msg = body.get("output", {}).get("message", {})
        content_blocks = output_msg.get("content", [])
        generation = content_blocks[0].get("text", "") if content_blocks else ""
        usage = body.get("usage", {})
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
    except Exception as exc:
        latency = time.monotonic() - t0
        logger.error(
            "Bedrock Tier 2 invocation failed",
            error=str(exc),
            latency_s=f"{latency:.2f}",
        )
        # Timeout / error → fall back to Tier 1 confidence as suspicion score
        logger.error(
            "Bedrock Tier 2 failed — using T1 fallback score",
            error=str(exc),
            error_type=type(exc).__name__,
            signal_id=signal.signal_id,
            fallback_score=t1_result.confidence,
        )
        return ReasoningResult(
            suspicion_score=t1_result.confidence,
            reasoning=f"Tier 2 failed ({exc}); using Tier 1 confidence as fallback.",
            key_evidence=t1_result.one_liner,
            model=model_id,
            input_tokens=0,
            output_tokens=0,
            is_fallback=True,
        )

    latency = time.monotonic() - t0
    result = parse_reasoning(generation, model=model_id)

    # Patch in real token counts
    result = ReasoningResult(
        suspicion_score=result.suspicion_score,
        reasoning=result.reasoning,
        key_evidence=result.key_evidence,
        model=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    logger.info(
        "Tier 2 reasoning complete",
        signal_id=signal.signal_id,
        suspicion_score=result.suspicion_score,
        latency_s=f"{latency:.2f}",
        tokens=input_tokens + output_tokens,
    )
    return result
