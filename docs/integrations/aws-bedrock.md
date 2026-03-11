# Integration: AWS Bedrock (LLM Intelligence Layer)

> The "Judge" — cross-references flagged trades against live news to produce a suspicion score and human-readable reasoning.

## Model Selection

### Two-Tier Strategy

| Tier | Model | Role | Cost/call | Daily Cap |
|------|-------|------|-----------|-----------|
| **1 — Classifier** | Amazon Nova Lite | Quick “Informed vs Noise” classification | ~$0.0001 | 200 |
| **2 — Reasoner** | Amazon Nova Pro | Detailed reasoning for high-suspicion trades | ~$0.002 | 30 |

See [ADR-003](../architecture/adr-003-bedrock-budget.md) for full cost analysis.

### Why Bedrock Over Direct API?

- **Single bill**: All LLM costs on one AWS invoice
- **No API key management**: IAM role-based auth
- **Model switching**: Change models without code changes (just update the model ID)
- **Guardrails**: Bedrock Guardrails can filter/block inappropriate outputs (not critical for us, but free)

## Setup

### Prerequisites

1. AWS account with Bedrock access enabled in your region (us-east-1 or us-west-2)
2. Request model access for:
   - `amazon.nova-lite-v1:0`
   - `amazon.nova-pro-v1:0`
3. IAM user/role with `bedrock:InvokeModel` permission

### IAM Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0",
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"
      ]
    }
  ]
}
```

## Python Client

```python
import boto3
import json
from sentinel.config import settings

bedrock = boto3.client(
    "bedrock-runtime",
    region_name=settings.aws_region,
    aws_access_key_id=settings.aws_access_key_id,
    aws_secret_access_key=settings.aws_secret_access_key,
)


def invoke_nova_lite(messages: list[dict], system: str = "") -> dict:
    """Tier 1: Quick classification."""
    body = {
        "schemaVersion": "messages-v1",
        "messages": messages,
        "inferenceConfig": {
            "maxTokens": 256,
            "temperature": 0.1,
            "topP": 0.9,
        },
    }
    if system:
        body["system"] = [{"text": system}]
    response = bedrock.invoke_model(
        modelId=settings.bedrock_tier1_model,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body),
    )
    return json.loads(response["body"].read())


def invoke_nova_pro(messages: list[dict]) -> dict:
    """Tier 2: Deep reasoning."""
    response = bedrock.invoke_model(
        modelId=settings.bedrock_tier2_model,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "schemaVersion": "messages-v1",
            "messages": messages,
            "inferenceConfig": {
                "maxTokens": 512,
                "temperature": 0.2,
            },
        }),
    )
    return json.loads(response["body"].read())
```

## Prompt Design

### Tier 1 — Classifier Prompt (Amazon Nova Lite)

Both tiers use the Amazon Nova `messages-v1` schema. The system prompt is passed
via the `system` array and the trade context as a `user` message with content blocks:

**System:**
```
You are a prediction market analyst. Classify whether this trade is likely
"INFORMED" (based on private/early information) or "NOISE" (retail speculation).

Respond with EXACTLY this JSON:
{"classification": "INFORMED"|"NOISE", "confidence": 0-100, "one_liner": "..."}
```

**User:**
```
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

Is this trade INFORMED or NOISE?
```

**Response format (Nova):**
```json
{
  "output": {
    "message": {
      "role": "assistant",
      "content": [{"text": "{\"classification\": ...}"}]
    }
  },
  "usage": {"inputTokens": 150, "outputTokens": 40}
}
```

### Tier 2 — Reasoner Prompt (Amazon Nova Pro)

```json
{
  "messages": [
    {
      "role": "user",
      "content": "You are a senior prediction market intelligence analyst.\n\nA trade has been flagged by our statistical system:\n\nMARKET: \"{market_question}\"\nCATEGORY: {category}\nTRADE: {side} ${size_usd} at {price} (implied prob: {price*100}%)\nWALLET: Win rate {win_rate} across {total_trades} resolved markets\nSTATISTICAL ANOMALY: Volume Z-score {z_score}, funded {funding_age_minutes}min before trade\n\nNEWS CONTEXT (last 24h):\n{numbered_headlines}\n\nANALYSIS REQUIRED:\n1. Is this trade's timing suspiciously early relative to the news?\n2. Could the trader have information not yet reflected in headlines?\n3. Rate suspicion 1-100 (100 = almost certainly informed)\n\nRespond as JSON:\n{\"suspicion_score\": N, \"reasoning\": \"Two sentences explaining your assessment.\", \"key_evidence\": \"The single most important factor.\"}"
    }
  ]
}
```

## Error Handling

```python
from botocore.exceptions import ClientError

try:
    result = invoke_nova_lite(messages, system=system_prompt)
except ClientError as e:
    error_code = e.response["Error"]["Code"]
    if error_code == "ThrottlingException":
        # Back off and retry
        await asyncio.sleep(5)
    elif error_code == "ModelTimeoutException":
        # Log and skip — don't block the pipeline
        logger.warning(f"Bedrock timeout for trade {trade_id}")
    elif error_code == "AccessDeniedException":
        # Model not enabled — fatal
        raise
```

## Environment Variables

```
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
BEDROCK_TIER1_MODEL=amazon.nova-lite-v1:0
BEDROCK_TIER1_DAILY_LIMIT=200
BEDROCK_TIER2_MODEL=amazon.nova-pro-v1:0
BEDROCK_TIER2_DAILY_LIMIT=30
BEDROCK_TIER2_MIN_SUSPICION=60
```

## Testing

- **Unit**: Mock `boto3.client` with `moto` or `unittest.mock`. Assert prompt structure and response parsing.
- **Integration**: Call Bedrock with a canned trade. Assert response parses to valid JSON with required fields.
- **Budget**: Test that the 201st call in a day is rejected and queued.
