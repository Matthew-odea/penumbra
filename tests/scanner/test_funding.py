"""Tests for sentinel.scanner.funding — Alchemy funding anomaly checker."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from sentinel.scanner.funding import (
    FundingResult,
    _set_cached,
    check_funding_anomaly,
    clear_cache,
)


@pytest.fixture(autouse=True)
def _clear_funding_cache():
    """Ensure a clean cache for every test."""
    clear_cache()
    yield
    clear_cache()


class TestFundingAnomalyChecker:
    @pytest.mark.asyncio
    async def test_no_alchemy_key_skips(self):
        """When alchemy_api_key is empty, should return non-anomaly."""
        with patch("sentinel.scanner.funding.settings") as mock_settings:
            mock_settings.alchemy_api_key = ""
            mock_settings.funding_anomaly_threshold_minutes = 60
            result = await check_funding_anomaly("0xtest", datetime.now(tz=UTC))
            assert result.is_anomaly is False
            assert result.funding_age_minutes is None

    @pytest.mark.asyncio
    async def test_cache_hit(self):
        """Cached result should be returned without API call."""
        wallet = "0xcached"
        cached_result = FundingResult(
            wallet=wallet,
            is_anomaly=True,
            funding_age_minutes=5,
            first_funding_tx="0xtx",
            checked_at=datetime.now(tz=UTC),
        )
        _set_cached(wallet, cached_result)

        result = await check_funding_anomaly(wallet, datetime.now(tz=UTC))
        assert result.is_anomaly is True
        assert result.funding_age_minutes == 5

    @pytest.mark.asyncio
    async def test_api_error_returns_safe_default(self):
        """API exceptions should be caught and return non-anomaly."""
        with patch("sentinel.scanner.funding.settings") as mock_settings:
            mock_settings.alchemy_api_key = "test-key"
            mock_settings.effective_alchemy_url = "https://fake.alchemy.com"
            mock_settings.funding_anomaly_threshold_minutes = 60
            with patch("sentinel.scanner.funding._query_alchemy", side_effect=Exception("boom")):
                result = await check_funding_anomaly("0xerror", datetime.now(tz=UTC))
                assert result.is_anomaly is False

    @pytest.mark.asyncio
    async def test_fresh_wallet_detected(self):
        """Wallet funded 10 minutes before trade → anomaly."""
        trade_time = datetime.now(tz=UTC)
        funding_time = trade_time - timedelta(minutes=10)

        mock_result = FundingResult(
            wallet="0xfresh",
            is_anomaly=True,
            funding_age_minutes=10,
            first_funding_tx="0xtx",
            checked_at=datetime.now(tz=UTC),
        )
        with patch("sentinel.scanner.funding.settings") as mock_settings:
            mock_settings.alchemy_api_key = "test-key"
            mock_settings.effective_alchemy_url = "https://fake.alchemy.com"
            mock_settings.funding_anomaly_threshold_minutes = 60
            with patch("sentinel.scanner.funding._query_alchemy", return_value=mock_result):
                result = await check_funding_anomaly("0xfresh", trade_time)
                assert result.is_anomaly is True
                assert result.funding_age_minutes == 10

    @pytest.mark.asyncio
    async def test_old_wallet_not_anomaly(self):
        """Wallet funded 90 minutes before trade → not anomaly."""
        mock_result = FundingResult(
            wallet="0xold",
            is_anomaly=False,
            funding_age_minutes=90,
            first_funding_tx="0xtx",
            checked_at=datetime.now(tz=UTC),
        )
        with patch("sentinel.scanner.funding.settings") as mock_settings:
            mock_settings.alchemy_api_key = "test-key"
            mock_settings.effective_alchemy_url = "https://fake.alchemy.com"
            mock_settings.funding_anomaly_threshold_minutes = 60
            with patch("sentinel.scanner.funding._query_alchemy", return_value=mock_result):
                result = await check_funding_anomaly("0xold", datetime.now(tz=UTC))
                assert result.is_anomaly is False

    def test_funding_result_dataclass(self):
        fr = FundingResult(
            wallet="0x",
            is_anomaly=True,
            funding_age_minutes=5,
            first_funding_tx="0xtx",
            checked_at=datetime.now(tz=UTC),
        )
        assert fr.is_anomaly is True
        assert fr.funding_age_minutes == 5
