"""Tier 1 — Amazon Nova Lite classifier via AWS Bedrock.

Performs a quick INFORMED/NOISE binary classification with a confidence
score.  Signals whose confidence ≥ ``settings.bedrock_tier2_min_suspicion``
are promoted to the Tier 2 reasoner.
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
from sentinel.scanner.scorer import Signal

logger = structlog.get_logger()

# ── Data classes ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClassificationInput:
    """Everything the Tier 1 prompt needs."""

    market_question: str
    category: str
    side: str
    price: float
    size_usd: float
    liquidity_usd: float
    z_score: float
    wallet_win_rate: float | None
    wallet_total_trades: int
    funding_age_minutes: int | None
    news_headlines: str


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Parsed output from Amazon Nova Lite."""

    classification: str  # "INFORMED" or "NOISE"
    confidence: int  # 0-100
    one_liner: str  # Brief explanation
    model: str
    input_tokens: int
    output_tokens: int


# ── Prompt construction ─────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a prediction market analyst. Classify whether this trade is likely \
"INFORMED" (based on private/early information) or "NOISE" (retail speculation).

Respond with EXACTLY this JSON:
{"classification": "INFORMED"|"NOISE", "confidence": 0-100, "one_liner": "..."}"""

_USER_TEMPLATE = """\
TRADE CONTEXT:
- Market: "{market_question}"
- Category: {category}
- Side: {side} at ${price} for ${size_usd}
- Market liquidity: ${liquidity_usd}
- Volume Z-score: {z_score} (vs 24h norm)
- Wallet win rate: {win_rate} ({total_trades} historical trades)
- Wallet funded {funding_age_minutes} minutes before this trade

RECENT NEWS (last 24h):
{news_headlines}

Is this trade INFORMED or NOISE?"""


def build_prompt(inp: ClassificationInput) -> list[dict]:
    """Build Amazon Nova Messages payload from a ``ClassificationInput``.

    Returns a list of message dicts in Nova ``messages-v1`` format.
    The system prompt is passed separately via the ``system`` field in invoke.
    """
    user_block = _USER_TEMPLATE.format(
        market_question=inp.market_question,
        category=inp.category,
        side=inp.side,
        price=inp.price,
        size_usd=inp.size_usd,
        liquidity_usd=inp.liquidity_usd,
        z_score=round(inp.z_score, 2),
        win_rate=f"{inp.wallet_win_rate:.1%}" if inp.wallet_win_rate else "unknown",
        total_trades=inp.wallet_total_trades,
        funding_age_minutes=inp.funding_age_minutes if inp.funding_age_minutes else "N/A",
        news_headlines=inp.news_headlines,
    )
    return [{"role": "user", "content": [{"text": user_block}]}]


def build_input_from_signal(
    signal: Signal,
    *,
    market_question: str = "",
    category: str = "",
    liquidity_usd: float = 0.0,
    news_headlines: str = "No relevant news found.",
) -> ClassificationInput:
    """Convenience: build a ``ClassificationInput`` from a ``Signal``."""
    return ClassificationInput(
        market_question=market_question,
        category=category,
        side=signal.side,
        price=signal.price,
        size_usd=signal.size_usd,
        liquidity_usd=liquidity_usd,
        z_score=signal.modified_z_score,
        wallet_win_rate=signal.wallet_win_rate,
        wallet_total_trades=signal.wallet_total_trades or 0,
        funding_age_minutes=signal.funding_age_minutes,
        news_headlines=news_headlines,
    )


# ── Response parsing ────────────────────────────────────────────────────────

_JSON_PATTERN = re.compile(r"\{[^{}]*\}")


def parse_classification(raw: str, *, model: str = "") -> ClassificationResult:
    """Extract a ``ClassificationResult`` from the LLM's raw text response.

    Tries strict ``json.loads`` first, then regex extraction, then falls
    back to a conservative NOISE result.
    """
    # 1. Try direct JSON parse
    try:
        obj = json.loads(raw.strip())
        return _result_from_dict(obj, model=model)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass

    # 2. Try regex extraction of first JSON object
    match = _JSON_PATTERN.search(raw)
    if match:
        try:
            obj = json.loads(match.group())
            return _result_from_dict(obj, model=model)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

    # 3. Fallback — conservative default
    logger.warning("Failed to parse Tier 1 response, using fallback", raw=raw[:200])
    return ClassificationResult(
        classification="NOISE",
        confidence=0,
        one_liner="Unable to parse model response",
        model=model,
        input_tokens=0,
        output_tokens=0,
    )


def _result_from_dict(obj: dict[str, Any], *, model: str) -> ClassificationResult:
    classification = str(obj.get("classification", "NOISE")).upper()
    if classification not in ("INFORMED", "NOISE"):
        classification = "NOISE"
    confidence = int(obj.get("confidence", 0))
    confidence = max(0, min(100, confidence))
    one_liner = str(obj.get("one_liner", ""))
    return ClassificationResult(
        classification=classification,
        confidence=confidence,
        one_liner=one_liner,
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
    IAM instance profile → ~/.aws/credentials), which supports local dev,
    EC2/ECS IAM roles, and GitHub OIDC in CI without hardcoding secrets.
    """
    global _bedrock_client
    if _bedrock_client is None:
        with _bedrock_client_lock:
            if _bedrock_client is None:
                import boto3
                _bedrock_client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
    return _bedrock_client


async def classify(
    signal: Signal,
    *,
    news_headlines: str = "No relevant news found.",
    market_question: str = "",
    category: str = "",
    liquidity_usd: float = 0.0,
    bedrock_client: Any | None = None,
) -> ClassificationResult:
    """Run Tier 1 classification on a signal.

    Args:
        signal: Scored signal from the scanner.
        news_headlines: Pre-formatted headline string.
        market_question: Market question text.
        category: Market category.
        liquidity_usd: Market liquidity.
        bedrock_client: Optional pre-built boto3 client (for testing).

    Returns:
        ClassificationResult with the Amazon Nova Lite assessment.
    """
    inp = build_input_from_signal(
        signal,
        market_question=market_question,
        category=category,
        liquidity_usd=liquidity_usd,
        news_headlines=news_headlines,
    )
    messages = build_prompt(inp)
    model_id = settings.bedrock_tier1_model

    client = bedrock_client or _get_bedrock_client()

    t0 = time.monotonic()
    try:
        response = client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "schemaVersion": "messages-v1",
                "system": [{"text": _SYSTEM_PROMPT}],
                "messages": messages,
                "inferenceConfig": {
                    "maxTokens": 256,
                    "temperature": 0.1,
                    "topP": 0.9,
                },
            }),
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
        logger.error("Bedrock Tier 1 invocation failed", error=str(exc), latency_s=f"{latency:.2f}")
        return ClassificationResult(
            classification="NOISE",
            confidence=0,
            one_liner=f"Bedrock error: {exc}",
            model=model_id,
            input_tokens=0,
            output_tokens=0,
        )

    latency = time.monotonic() - t0
    result = parse_classification(generation, model=model_id)

    # Patch in token counts from the actual response
    result = ClassificationResult(
        classification=result.classification,
        confidence=result.confidence,
        one_liner=result.one_liner,
        model=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    logger.info(
        "Tier 1 classification complete",
        signal_id=signal.signal_id,
        classification=result.classification,
        confidence=result.confidence,
        latency_s=f"{latency:.2f}",
        tokens=input_tokens + output_tokens,
    )
    return result
