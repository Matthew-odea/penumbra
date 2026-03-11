"""Tests for sentinel.scanner.scorer — composite signal scoring."""

from datetime import UTC, datetime

import pytest

from sentinel.scanner.scorer import (
    Signal,
    build_signal,
    compute_statistical_score,
)


class TestComputeStatisticalScore:
    """Unit tests for the scoring formula."""

    def test_all_zeros(self):
        """No signals at all → score 0."""
        score = compute_statistical_score(
            z_score=0.0,
            price_impact=0.0,
            win_rate=None,
            is_whitelisted=False,
            funding_anomaly=False,
            funding_age_minutes=None,
            zscore_threshold=3.5,
        )
        assert score == 0

    def test_volume_only_at_threshold(self):
        """Z-score exactly at threshold → 0 points (needs to exceed)."""
        score = compute_statistical_score(
            z_score=3.5,
            price_impact=0.0,
            win_rate=None,
            is_whitelisted=False,
            funding_anomaly=False,
            funding_age_minutes=None,
            zscore_threshold=3.5,
        )
        assert score == 0

    def test_volume_above_threshold(self):
        """Z-score = 5.5 (2.0 above 3.5) → (2.0 * 10) = 20 points."""
        score = compute_statistical_score(
            z_score=5.5,
            price_impact=0.0,
            win_rate=None,
            is_whitelisted=False,
            funding_anomaly=False,
            funding_age_minutes=None,
            zscore_threshold=3.5,
        )
        assert score == 20

    def test_volume_capped_at_40(self):
        """Extremely high Z-score should be capped at 40 points."""
        score = compute_statistical_score(
            z_score=20.0,
            price_impact=0.0,
            win_rate=None,
            is_whitelisted=False,
            funding_anomaly=False,
            funding_age_minutes=None,
            zscore_threshold=3.5,
        )
        assert score == 40

    def test_price_impact_scoring(self):
        """Price impact = 0.01 → int(0.01 * 1000) = 10 points."""
        score = compute_statistical_score(
            z_score=0.0,
            price_impact=0.01,
            win_rate=None,
            is_whitelisted=False,
            funding_anomaly=False,
            funding_age_minutes=None,
            zscore_threshold=3.5,
        )
        assert score == 10

    def test_price_impact_capped_at_20(self):
        score = compute_statistical_score(
            z_score=0.0,
            price_impact=0.05,
            win_rate=None,
            is_whitelisted=False,
            funding_anomaly=False,
            funding_age_minutes=None,
            zscore_threshold=3.5,
        )
        assert score == 20

    def test_whitelisted_wallet(self):
        """Whitelisted wallet → 20 points."""
        score = compute_statistical_score(
            z_score=0.0,
            price_impact=0.0,
            win_rate=0.8,
            is_whitelisted=True,
            funding_anomaly=False,
            funding_age_minutes=None,
            zscore_threshold=3.5,
        )
        assert score == 20

    def test_high_win_rate_not_whitelisted(self):
        """Win rate 0.7 (> 0.6) but not whitelisted → int(0.7 * 20) = 14."""
        score = compute_statistical_score(
            z_score=0.0,
            price_impact=0.0,
            win_rate=0.7,
            is_whitelisted=False,
            funding_anomaly=False,
            funding_age_minutes=None,
            zscore_threshold=3.5,
        )
        assert score == 14

    def test_low_win_rate(self):
        """Win rate 0.5 (< 0.6) → 0 wallet points."""
        score = compute_statistical_score(
            z_score=0.0,
            price_impact=0.0,
            win_rate=0.5,
            is_whitelisted=False,
            funding_anomaly=False,
            funding_age_minutes=None,
            zscore_threshold=3.5,
        )
        assert score == 0

    def test_funding_anomaly_very_fresh(self):
        """Funding < 15 min → 20 points."""
        score = compute_statistical_score(
            z_score=0.0,
            price_impact=0.0,
            win_rate=None,
            is_whitelisted=False,
            funding_anomaly=True,
            funding_age_minutes=5,
            zscore_threshold=3.5,
        )
        assert score == 20

    def test_funding_anomaly_moderate(self):
        """Funding 15-60 min → 10 points."""
        score = compute_statistical_score(
            z_score=0.0,
            price_impact=0.0,
            win_rate=None,
            is_whitelisted=False,
            funding_anomaly=True,
            funding_age_minutes=30,
            zscore_threshold=3.5,
        )
        assert score == 10

    def test_funding_anomaly_false(self):
        """No anomaly → 0 funding points even with age."""
        score = compute_statistical_score(
            z_score=0.0,
            price_impact=0.0,
            win_rate=None,
            is_whitelisted=False,
            funding_anomaly=False,
            funding_age_minutes=5,
            zscore_threshold=3.5,
        )
        assert score == 0

    def test_composite_max(self):
        """All signals maxed → capped at 100."""
        score = compute_statistical_score(
            z_score=20.0,       # 40 pts
            price_impact=0.1,   # 20 pts
            win_rate=0.9,
            is_whitelisted=True,  # 20 pts
            funding_anomaly=True,
            funding_age_minutes=3,  # 20 pts
            zscore_threshold=3.5,
        )
        assert score == 100

    def test_composite_mixed(self):
        """Realistic mixed scenario."""
        score = compute_statistical_score(
            z_score=5.0,        # (5.0-3.5)*10 = 15 pts
            price_impact=0.005, # int(0.005*1000) = 5 pts
            win_rate=0.7,
            is_whitelisted=False,  # int(0.7*20) = 14 pts
            funding_anomaly=True,
            funding_age_minutes=10,  # 20 pts
            zscore_threshold=3.5,
        )
        assert score == 54  # 15 + 5 + 14 + 20

    def test_custom_threshold(self):
        """Custom threshold changes volume scoring."""
        score = compute_statistical_score(
            z_score=3.0,
            price_impact=0.0,
            win_rate=None,
            is_whitelisted=False,
            funding_anomaly=False,
            funding_age_minutes=None,
            zscore_threshold=2.0,  # Lower threshold
        )
        assert score == 10  # (3.0 - 2.0) * 10 = 10


class TestBuildSignal:
    def test_build_signal_basic(self):
        now = datetime.now(tz=UTC)
        signal = build_signal(
            trade_id="t1",
            market_id="m1",
            wallet="0xwallet",
            side="BUY",
            price=0.73,
            size_usd=5000.0,
            trade_timestamp=now,
            modified_z_score=5.5,
            price_impact=0.01,
        )
        assert isinstance(signal, Signal)
        assert signal.statistical_score == 30  # 20 (vol) + 10 (impact)
        assert signal.trade_id == "t1"
        assert signal.signal_id  # UUID assigned

    def test_build_signal_below_threshold(self):
        now = datetime.now(tz=UTC)
        signal = build_signal(
            trade_id="t2",
            market_id="m1",
            wallet="0x",
            side="SELL",
            price=0.5,
            size_usd=100.0,
            trade_timestamp=now,
        )
        assert signal.statistical_score == 0

    def test_signal_as_dict(self):
        now = datetime.now(tz=UTC)
        signal = build_signal(
            trade_id="t1",
            market_id="m1",
            wallet="0xabcdef1234567890",
            side="BUY",
            price=0.73,
            size_usd=5000.0,
            trade_timestamp=now,
            is_whitelisted=True,
        )
        d = signal.as_dict()
        assert d["side"] == "BUY"
        assert d["is_whitelisted"] is True
        assert "..." in d["wallet"]  # Truncated

    def test_signal_as_db_tuple(self):
        now = datetime.now(tz=UTC)
        signal = build_signal(
            trade_id="t1",
            market_id="m1",
            wallet="0x",
            side="BUY",
            price=0.5,
            size_usd=100.0,
            trade_timestamp=now,
        )
        t = signal.as_db_tuple()
        assert len(t) == 18  # 18 columns in signals table
