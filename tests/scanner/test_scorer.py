"""Tests for sentinel.scanner.scorer — composite signal scoring."""

from datetime import UTC, datetime

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
        """Price impact = 0.01 → log10(0.01)+4=2, 2*4=8 points."""
        score = compute_statistical_score(
            z_score=0.0,
            price_impact=0.01,
            win_rate=None,
            is_whitelisted=False,
            funding_anomaly=False,
            funding_age_minutes=None,
            zscore_threshold=3.5,
        )
        assert score == 8

    def test_price_impact_capped_at_20(self):
        """Price impact = 10.0 → log10(10)+4=5, 5*4=20 (capped at weight)."""
        score = compute_statistical_score(
            z_score=0.0,
            price_impact=10.0,
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
        """Win rate 0.7 → smooth ramp: (0.7-0.5)/0.5=0.4, 0.4*20=8."""
        score = compute_statistical_score(
            z_score=0.0,
            price_impact=0.0,
            win_rate=0.7,
            is_whitelisted=False,
            funding_anomaly=False,
            funding_age_minutes=None,
            zscore_threshold=3.5,
        )
        assert score == 8

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
        """Funding 15-60 min → int(20 * 0.75) = 15 points."""
        score = compute_statistical_score(
            z_score=0.0,
            price_impact=0.0,
            win_rate=None,
            is_whitelisted=False,
            funding_anomaly=True,
            funding_age_minutes=30,
            zscore_threshold=3.5,
        )
        assert score == 15

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
        """All signals maxed → 100."""
        score = compute_statistical_score(
            z_score=20.0,        # 40 pts (capped)
            price_impact=10.0,   # 20 pts (log10(10)+4=5, 5*4=20, capped)
            win_rate=0.9,
            is_whitelisted=True, # 20 pts
            funding_anomaly=True,
            funding_age_minutes=3,  # 20 pts
            zscore_threshold=3.5,
        )
        assert score == 100

    def test_composite_mixed(self):
        """Realistic mixed scenario."""
        score = compute_statistical_score(
            z_score=5.0,        # (5.0-3.5)*10 = 15 pts
            price_impact=0.005, # log10(0.005)+4=1.699, 1.699*4=6.8 → 6 pts
            win_rate=0.7,
            is_whitelisted=False,  # (0.7-0.5)/0.5*20=8 pts
            funding_anomaly=True,
            funding_age_minutes=10,  # 20 pts
            zscore_threshold=3.5,
        )
        assert score == 49  # 15 + 6 + 8 + 20

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
        assert signal.statistical_score == 33  # 20 (vol: (5.5-3.5)*10) + 8 (impact: log) + 5 (zero-history bonus)
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
        assert len(t) == 27  # 27 columns (incl. vpin_percentile, lambda_value)
