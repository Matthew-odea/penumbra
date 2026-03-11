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
        assert s.zscore_threshold == 3.5
        assert s.bedrock_tier1_daily_limit == 200
        assert s.bedrock_tier2_daily_limit == 30
        assert s.alert_min_score == 80
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
