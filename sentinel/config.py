"""Centralized settings for Penumbra.

All configuration is loaded from environment variables (or .env file).
Use `from sentinel.config import settings` anywhere in the codebase.
"""

from pathlib import Path

from pydantic import model_validator
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
    polymarket_data_api_url: str = "https://data-api.polymarket.com"
    # L2 auth — set via `python scripts/setup_l2.py`
    polymarket_private_key: str = ""
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""

    # ── DuckDB ──────────────────────────────────────────────────────────────
    duckdb_path: Path = Path("data/sentinel.duckdb")

    # ── AWS Bedrock ─────────────────────────────────────────────────────────
    # Credentials are resolved via the boto3 default chain:
    # env vars (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN)
    # → IAM instance profile → ~/.aws/credentials
    # This supports local dev, EC2/ECS IAM roles, and GitHub OIDC in CI.
    aws_region: str = "us-east-1"
    bedrock_tier1_model: str = "amazon.nova-lite-v1:0"
    bedrock_market_scoring_daily_limit: int = 50000  # Market attractiveness scoring budget

    # ── Polygon / Alchemy ───────────────────────────────────────────────────
    polygon_rpc_url: str = "https://polygon-rpc.com"
    alchemy_api_key: str = ""
    alchemy_polygon_url: str = ""
    funding_anomaly_threshold_minutes: int = 4320  # 72 hours (tiered decay applied in scorer)
    new_wallet_large_trade_multiplier: float = 5.0  # trade > min_trade_size * this → suspicious

    # ── Alerts ───────────────────────────────────────────────────────────
    alert_min_score: int = 80

    # ── Scanner Thresholds ──────────────────────────────────────────────────
    zscore_threshold: float = 2.0
    # Fallback market liquidity when Polymarket API returns null (stored as 0.0).
    # Used as denominator in price impact formula so the component is never dead.
    price_impact_fallback_liquidity_usd: float = 10_000.0
    min_trade_size_usd: float = 100.0
    signal_min_score: int = 30
    wallet_min_trades: int = 5
    wallet_whitelist_win_rate: float = 0.65
    wallet_whitelist_min_trades: int = 20

    # VPIN parameters (Plan B Phase 1)
    vpin_bucket_divisor: int = 50          # avg_daily_volume / divisor = bucket size
    vpin_min_bucket_size: float = 100.0    # Minimum bucket size in USD
    vpin_lookback_buckets: int = 50        # Number of trailing buckets for VPIN
    vpin_min_buckets: int = 5              # Minimum completed buckets before reporting VPIN

    # Kyle's Lambda parameters (Plan B Phase 1)
    lambda_min_observations: int = 6       # Minimum 5-min windows for OLS (30 min of data)
    lambda_window_minutes: int = 60        # Rolling window for Lambda estimation

    # Scorer weight caps (points out of 100) — must sum to 100
    scorer_weight_volume: int = 40
    scorer_weight_impact: int = 20
    scorer_weight_wallet: int = 20
    scorer_weight_funding: int = 20

    @model_validator(mode="after")
    def validate_scorer_weights(self) -> "Settings":
        total = (
            self.scorer_weight_volume
            + self.scorer_weight_impact
            + self.scorer_weight_wallet
            + self.scorer_weight_funding
        )
        if total != 100:
            raise ValueError(f"Scorer weights must sum to 100, got {total}")
        return self

    # ── Market Intelligence ──────────────────────────────────────────────────
    hot_market_count: int = 100                 # Size of hot polling tier (REST poller)
    ws_market_count: int = 500                  # WS subscription breadth (wider than REST hot tier)
    hot_market_min_score: int = 60              # Attractiveness threshold for hot tier
    hot_market_min_liquidity: float = 0.0       # 0: Polymarket API returns liquidity=None→0.0; priority formula deprioritises illiquid markets naturally
    hot_market_refresh_interval_seconds: int = 1800  # 30 min

    # ── Ingester ────────────────────────────────────────────────────────────
    ingester_batch_size: int = 20
    ingester_flush_interval_seconds: int = 1
    coordination_wallet_count_min: int = 3  # min distinct wallets to flag coordination
    market_sync_interval_hours: int = 2     # Sync all markets every 2h (was 6h)
    trade_poll_interval_seconds: int = 5
    trade_poll_limit: int = 1000

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
