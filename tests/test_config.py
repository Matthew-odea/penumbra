"""Test configuration loading."""

import os

from sentinel.config import Settings


class TestSettings:
    """Test that settings load correctly from defaults and env vars."""

    def test_defaults(self) -> None:
        """Settings have sensible defaults."""
        s = Settings(
            _env_file=None,  # Don't load .env in tests
        )
        assert s.duckdb_path.name == "sentinel.duckdb"
        assert s.zscore_threshold == 2.0
        assert s.bedrock_tier1_daily_limit == 5000  # High-throughput mode
        assert s.bedrock_tier2_daily_limit == 0  # Disabled by default
        assert s.judge_max_workers == 8  # Parallel processing
        assert s.alert_min_score == 80
        assert s.news_cache_ttl_hours == 12  # Extended cache
        assert s.news_min_score == 70  # Only fetch for high-scoring signals
        assert "Biotech" in s.polymarket_categories

    def test_alchemy_url_resolution(self) -> None:
        """Alchemy URL is correctly constructed from API key."""
        s = Settings(
            _env_file=None,
            alchemy_api_key="test-key-123",
            alchemy_polygon_url="",
        )
        assert "test-key-123" in s.effective_alchemy_url

    def test_alchemy_url_fallback(self) -> None:
        """Falls back to public RPC when no Alchemy key."""
        s = Settings(
            _env_file=None,
            alchemy_api_key="",
            alchemy_polygon_url="",
        )
        assert s.effective_alchemy_url == "https://polygon-rpc.com"
