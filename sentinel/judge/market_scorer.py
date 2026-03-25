"""Market attractiveness scoring via Amazon Nova Lite.

Scores each Polymarket market once (on first sync) on a 0-100 scale reflecting
how likely the market can be insider-traded — i.e. whether private information
could create an edge for a trader.

Examples:
  90 — "Will Trump bomb Iran?" (government intelligence)
  85 — "Will FDA approve Moderna's mRNA-1283?" (clinical trial data)
  60 — "Will the Fed cut rates in June?" (macro, some private edge)
  15 — "Will BTC close above $64K this week?" (public price feeds)
   5 — "Will BTC be above $64K in 5 minutes?" (pure noise)

The score is stored in ``markets.attractiveness_score`` and never re-computed
unless the market question changes.  The ``reason`` field gives a one-sentence
explanation surfaced in the dashboard Watchlist view.
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

logger = structlog.get_logger()


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MarketAttractivenessInput:
    """Everything the scoring prompt needs."""

    question: str
    tags: str          # Raw comma-joined tags from Polymarket
    end_date_str: str  # ISO string or "unknown"
    liquidity_usd: float


@dataclass(frozen=True, slots=True)
class MarketAttractivenessResult:
    """Parsed output from Nova Lite."""

    score: int        # 0-100
    reason: str       # One-sentence explanation
    model: str
    input_tokens: int
    output_tokens: int


# ── Prompt ───────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a prediction market analyst specialising in information asymmetry.
Score how likely this market can be profitably traded using private information
that is not yet public. Consider: who could have advance knowledge, how
actionable that knowledge would be, and whether the resolution is based on
verifiable facts vs. pure price/chance events.

Respond with EXACTLY this JSON (no other text):
{"score": 0-100, "reason": "one sentence"}"""

_USER_TEMPLATE = """\
Market: "{question}"
Tags: {tags}
Resolves: {end_date_str}
Liquidity: ${liquidity_usd}

Score examples for calibration:
- "Will Trump order a military strike on Iran before June?" → 92 \
(government officials have advance knowledge)
- "Will the FDA approve Novo Nordisk's semaglutide NDA?" → 87 \
(clinical trial insiders)
- "Will Russia and Ukraine sign a ceasefire by August?" → 72 \
(diplomatic sources)
- "Will the S&P 500 be above 5,200 at end of month?" → 18 \
(public market data, no asymmetry)
- "Will BTC be above $64,000 in 5 minutes?" → 3 \
(pure noise, no information edge possible)

What score does this market deserve?"""


def _build_prompt(inp: MarketAttractivenessInput) -> list[dict]:
    user_text = _USER_TEMPLATE.format(
        question=inp.question,
        tags=inp.tags or "none",
        end_date_str=inp.end_date_str,
        liquidity_usd=f"{inp.liquidity_usd:,.0f}",
    )
    return [{"role": "user", "content": [{"text": user_text}]}]


# ── Response parsing ─────────────────────────────────────────────────────────

_JSON_PATTERN = re.compile(r"\{[^{}]*\}")


def _parse_result(raw: str, *, model: str) -> MarketAttractivenessResult:
    """Extract score + reason from LLM response, with fallback."""
    try:
        obj = json.loads(raw.strip())
        return _result_from_dict(obj, model=model)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass

    match = _JSON_PATTERN.search(raw)
    if match:
        try:
            obj = json.loads(match.group())
            return _result_from_dict(obj, model=model)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

    logger.warning("Failed to parse market scoring response", raw=raw[:200])
    return MarketAttractivenessResult(
        score=50,
        reason="Unable to parse model response — defaulting to neutral score",
        model=model,
        input_tokens=0,
        output_tokens=0,
    )


def _result_from_dict(obj: dict[str, Any], *, model: str) -> MarketAttractivenessResult:
    score = int(obj.get("score", 50))
    score = max(0, min(100, score))
    reason = str(obj.get("reason", ""))[:500]
    return MarketAttractivenessResult(
        score=score,
        reason=reason,
        model=model,
        input_tokens=0,
        output_tokens=0,
    )


# ── Bedrock invocation ────────────────────────────────────────────────────────

_bedrock_client: Any = None
_bedrock_client_lock = threading.Lock()


def _get_bedrock_client() -> Any:
    global _bedrock_client
    if _bedrock_client is None:
        with _bedrock_client_lock:
            if _bedrock_client is None:
                import boto3
                _bedrock_client = boto3.client(
                    "bedrock-runtime", region_name=settings.aws_region
                )
    return _bedrock_client


async def score_market_attractiveness(
    inp: MarketAttractivenessInput,
    *,
    bedrock_client: Any | None = None,
) -> MarketAttractivenessResult:
    """Score a market's attractiveness for informed trading.

    Args:
        inp: Market details to score.
        bedrock_client: Optional pre-built boto3 client (for testing).

    Returns:
        MarketAttractivenessResult with score (0-100) and reason.
    """
    messages = _build_prompt(inp)
    model_id = settings.bedrock_tier1_model  # Reuse Nova Lite — cheap + fast
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
                    "maxTokens": 128,
                    "temperature": 0.1,
                    "topP": 0.9,
                },
            }),
        )
        body = json.loads(response["body"].read())
        output_msg = body.get("output", {}).get("message", {})
        content_blocks = output_msg.get("content", [])
        generation = content_blocks[0].get("text", "") if content_blocks else ""
        usage = body.get("usage", {})
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
    except Exception as exc:
        latency = time.monotonic() - t0
        logger.error(
            "Market scoring Bedrock call failed",
            error=str(exc),
            latency_s=f"{latency:.2f}",
        )
        return MarketAttractivenessResult(
            score=50,
            reason=f"Scoring failed: {exc}",
            model=model_id,
            input_tokens=0,
            output_tokens=0,
        )

    latency = time.monotonic() - t0
    result = _parse_result(generation, model=model_id)

    result = MarketAttractivenessResult(
        score=result.score,
        reason=result.reason,
        model=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    logger.debug(
        "Market scored",
        score=result.score,
        latency_s=f"{latency:.2f}",
        tokens=input_tokens + output_tokens,
    )
    return result
