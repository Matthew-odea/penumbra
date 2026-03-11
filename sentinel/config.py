"""Centralized settings for Penumbra.

All configuration is loaded from environment variables (or .env file).
Use `from sentinel.config import settings` anywhere in the codebase.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Polymarket ──────────────────────────────────────────────────────────
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polymarket_rest_url: str = "https://clob.polymarket.com"
    polymarket_categories: str = "Biotech,Politics,Crypto,Science"

    @property
    def categories_list(self) -> list[str]:
        """Parse comma-separated categories into a list."""
        return [s.strip() for s in self.polymarket_categories.split(",") if s.strip()]

    # ── DuckDB ──────────────────────────────────────────────────────────────
    duckdb_path: Path = Path("data/sentinel.duckdb")

    # ── Supabase ────────────────────────────────────────────────────────────
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_key: str = ""

    # ── AWS Bedrock ─────────────────────────────────────────────────────────
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    bedrock_tier1_model: str = "meta.llama3-8b-instruct-v1:0"
    bedrock_tier1_daily_limit: int = 200
    bedrock_tier2_model: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    bedrock_tier2_daily_limit: int = 30
    bedrock_tier2_min_suspicion: int = 60

    # ── Polygon / Alchemy ───────────────────────────────────────────────────
    polygon_rpc_url: str = "https://polygon-rpc.com"
    alchemy_api_key: str = ""
    alchemy_polygon_url: str = ""
    funding_anomaly_threshold_minutes: int = 60

    # ── Tavily / Exa Search ─────────────────────────────────────────────────
    tavily_api_key: str = ""
    exa_api_key: str = ""
    news_search_max_results: int = 5
    news_search_lookback_days: int = 3

    # ── Alerts ───────────────────────────────────────────────────────────
    alert_min_score: int = 80

    # ── Scanner Thresholds ──────────────────────────────────────────────────
    zscore_threshold: float = 3.5
    min_trade_size_usd: float = 500.0
    signal_min_score: int = 30
    wallet_min_trades: int = 5
    wallet_whitelist_win_rate: float = 0.65
    wallet_whitelist_min_trades: int = 20

    # ── Ingester ────────────────────────────────────────────────────────────
    ingester_batch_size: int = 100
    ingester_flush_interval_seconds: int = 5
    market_sync_interval_hours: int = 6

    # ── FastAPI ─────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    dashboard_origin: str = "http://localhost:3000"

    # ── Logging ─────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: str = "json"  # "json" or "console"

    @property
    def effective_alchemy_url(self) -> str:
        """Resolve Alchemy URL with API key substitution."""
        if self.alchemy_polygon_url:
            return self.alchemy_polygon_url.replace("${ALCHEMY_API_KEY}", self.alchemy_api_key)
        if self.alchemy_api_key:
            return f"https://polygon-mainnet.g.alchemy.com/v2/{self.alchemy_api_key}"
        return self.polygon_rpc_url  # Fallback to public RPC


# Singleton instance — import this everywhere
settings = Settings()
