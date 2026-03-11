# Integration: AWS Bedrock (LLM Intelligence Layer)

> The "Judge" — cross-references flagged trades against live news to produce a suspicion score and human-readable reasoning.

## Model Selection

### Two-Tier Strategy

| Tier | Model | Role | Cost/call | Daily Cap |
|------|-------|------|-----------|-----------|
| **1 — Classifier** | Llama 3 8B Instruct | Quick "Informed vs Noise" classification | ~$0.0004 | 200 |
| **2 — Reasoner** | Claude 3.5 Sonnet | Detailed reasoning for high-suspicion trades | ~$0.005 | 30 |

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
   - `meta.llama3-8b-instruct-v1:0`
   - `anthropic.claude-3-5-sonnet-20241022-v2:0`
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
        "arn:aws:bedrock:us-east-1::foundation-model/meta.llama3-8b-instruct-v1:0",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"
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


def invoke_llama3(prompt: str) -> dict:
    """Tier 1: Quick classification."""
    response = bedrock.invoke_model(
        modelId=settings.bedrock_tier1_model,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "prompt": prompt,
            "max_gen_len": 256,
            "temperature": 0.1,
            "top_p": 0.9,
        }),
    )
    return json.loads(response["body"].read())


def invoke_claude(messages: list[dict]) -> dict:
    """Tier 2: Deep reasoning."""
    response = bedrock.invoke_model(
        modelId=settings.bedrock_tier2_model,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 512,
            "temperature": 0.2,
            "messages": messages,
        }),
    )
    return json.loads(response["body"].read())
```

## Prompt Design

### Tier 1 — Classifier Prompt (Llama 3)

```
<|begin_of_turn|>system
You are a prediction market analyst. Classify whether this trade is likely
"INFORMED" (based on private/early information) or "NOISE" (retail speculation).

Respond with EXACTLY this JSON:
{"classification": "INFORMED"|"NOISE", "confidence": 0-100, "one_liner": "..."}
<|end_of_turn|>
<|begin_of_turn|>user
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
<|end_of_turn|>
```

### Tier 2 — Reasoner Prompt (Claude)

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
    result = invoke_llama3(prompt)
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
BEDROCK_TIER1_MODEL=meta.llama3-8b-instruct-v1:0
BEDROCK_TIER1_DAILY_LIMIT=200
BEDROCK_TIER2_MODEL=anthropic.claude-3-5-sonnet-20241022-v2:0
BEDROCK_TIER2_DAILY_LIMIT=30
BEDROCK_TIER2_MIN_SUSPICION=60
```

## Testing

- **Unit**: Mock `boto3.client` with `moto` or `unittest.mock`. Assert prompt structure and response parsing.
- **Integration**: Call Bedrock with a canned trade. Assert response parses to valid JSON with required fields.
- **Budget**: Test that the 201st call in a day is rejected and queued.
