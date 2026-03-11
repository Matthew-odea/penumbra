"""Sprint 2 — Statistical signal detection engine."""

from sentinel.scanner.funding import FundingResult, check_funding_anomaly
from sentinel.scanner.pipeline import Scanner
from sentinel.scanner.price_impact import PriceImpact, compute_impact_score, get_price_impact
from sentinel.scanner.scorer import Signal, build_signal, compute_statistical_score, write_signal
from sentinel.scanner.volume import VolumeAnomaly, get_anomalies, get_zscore_for_market
from sentinel.scanner.wallet_profiler import WalletProfile, get_wallet_profile, is_whitelisted

__all__ = [
    "FundingResult",
    "PriceImpact",
    "Scanner",
    "Signal",
    "VolumeAnomaly",
    "WalletProfile",
    "build_signal",
    "check_funding_anomaly",
    "compute_impact_score",
    "compute_statistical_score",
    "get_anomalies",
    "get_price_impact",
    "get_wallet_profile",
    "get_zscore_for_market",
    "is_whitelisted",
    "write_signal",
]
