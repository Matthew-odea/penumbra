# Integration: Tavily Search API (News Context)

> Provides real-time news context for the Intelligence Filter ("Judge").

## Purpose

When a trade is flagged, the Judge needs to determine if it's "Publicly Justified" (news already explains the price move) or "Suspiciously Early" (no public information supports the trade). Tavily provides structured search results optimized for LLM consumption.

## Why Tavily?

| Search API | LLM-Optimized | Cost | Speed |
|------------|--------------|------|-------|
| **Tavily** | Yes (returns clean text, not HTML) | 1,000 free/month, then $0.01/call | ~1-2s |
| Exa | Yes (neural search) | 1,000 free/month | ~2-3s |
| SerpAPI | No (Google scraping) | 100 free/month | ~3-5s |
| Direct Google | No | Free but fragile | Variable |

Tavily is purpose-built for AI agents — returns **clean, summarized text** instead of raw HTML, reducing token usage in the Bedrock prompt.

## Setup

```
TAVILY_API_KEY=tvly-...
```

Get a key at https://tavily.com

## Python Client

```python
from tavily import TavilyClient
from sentinel.config import settings


tavily = TavilyClient(api_key=settings.tavily_api_key)


async def get_news_context(market_question: str, category: str) -> list[dict]:
    """Fetch recent news related to a prediction market.
    
    Returns up to 5 headlines with summaries.
    """
    # Construct a targeted search query
    query = f"{market_question} latest news {category}"
    
    response = tavily.search(
        query=query,
        search_depth="basic",      # "basic" = faster, "advanced" = more thorough
        max_results=5,
        include_answer=False,       # We don't need Tavily's own summary
        include_raw_content=False,  # Save tokens — just titles + snippets
        days=3,                     # Only last 3 days
    )
    
    headlines = []
    for result in response.get("results", []):
        headlines.append({
            "title": result["title"],
            "snippet": result["content"][:200],  # Truncate to save tokens
            "url": result["url"],
            "published_date": result.get("published_date"),
            "relevance_score": result.get("score", 0),
        })
    
    return headlines


def format_headlines_for_prompt(headlines: list[dict]) -> str:
    """Format headlines for inclusion in a Bedrock prompt."""
    if not headlines:
        return "No relevant news found in the last 3 days."
    
    lines = []
    for i, h in enumerate(headlines, 1):
        date_str = h.get("published_date", "unknown date")
        lines.append(f"{i}. [{date_str}] {h['title']}")
        lines.append(f"   {h['snippet']}")
    
    return "\n".join(lines)
```

## Example Flow

```
Trade flagged: "Will FDA approve Drug X?" — $15k BUY at 0.82

→ Tavily search: "FDA approve Drug X latest news Biotech"

→ Results:
  1. [2026-03-10] "Drug X Phase 3 trial shows mixed results" — Reuters  
  2. [2026-03-09] "FDA advisory committee to meet March 15" — BioPharma Dive
  3. [2026-03-08] "Drug X developer stock rises 5%" — MarketWatch

→ Judge analysis: "The $15k BUY at 82% was placed 2 days BEFORE the advisory 
   committee meeting with no public catalyst. The mixed Phase 3 results would  
   typically suppress, not boost, confidence. Suspicion: 78/100."
```

## Rate Limits & Costs

| Tier | Calls/Month | Cost |
|------|-------------|------|
| Free | 1,000 | $0 |
| Basic | 10,000 | $50/month |
| Pro | 100,000 | $200/month |

### Our Usage

- Only called for flagged trades (post-Statistical Filter)
- Expected: 20-100 calls/day → **600-3,000/month**
- Free tier covers light usage; Basic tier for heavier markets

## Caching Strategy

To avoid repeat searches for the same market within a short window:

```python
from functools import lru_cache
from datetime import datetime

# Cache by market_id + hour (so news refreshes hourly)
@lru_cache(maxsize=200)
def _cache_key(market_id: str, hour: int):
    return (market_id, hour)

async def get_news_cached(market_id: str, market_question: str, category: str):
    hour = datetime.utcnow().hour
    cache_key = (market_id, hour)
    
    if cache_key in _news_cache:
        return _news_cache[cache_key]
    
    headlines = await get_news_context(market_question, category)
    _news_cache[cache_key] = headlines
    return headlines
```

## Fallback: Exa Search

If Tavily is down or rate-limited:

```python
from exa_py import Exa

exa = Exa(api_key=settings.exa_api_key)

results = exa.search_and_contents(
    query=f"{market_question} news",
    num_results=5,
    use_autoprompt=True,
    start_published_date="2026-03-08",  # Last 3 days
    text={"max_characters": 200},
)
```

## Environment Variables

```
TAVILY_API_KEY=tvly-...
EXA_API_KEY=exa-...          # Optional fallback
NEWS_SEARCH_MAX_RESULTS=5
NEWS_SEARCH_LOOKBACK_DAYS=3
```

## Testing

- **Unit**: Mock Tavily response, verify headline formatting for prompts
- **Integration**: Search for a known recent event, assert ≥1 relevant result
- **Token budget**: Assert formatted headlines ≤ 500 tokens
