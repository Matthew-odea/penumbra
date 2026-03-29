/* ── Domain types matching FastAPI responses ──────────────────────── */

export interface Signal {
  signal_id: string
  trade_id: string
  market_id: string
  wallet: string
  side: 'BUY' | 'SELL'
  price: number
  size_usd: number
  trade_timestamp: string
  modified_z_score: number | null
  price_impact: number | null
  wallet_win_rate: number | null
  wallet_total_trades: number | null
  is_whitelisted: boolean
  funding_anomaly: boolean
  funding_age_minutes: number | null
  statistical_score: number
  created_at: string
  // Enriched signals
  ofi_score: number | null
  hours_to_resolution: number | null
  market_concentration: number | null
  coordination_wallet_count: number | null
  liquidity_cliff: boolean | null
  position_trade_count: number | null
  // Template explanation (non-null for score >= 80)
  explanation: string | null
  // Market metadata
  market_question: string | null
  category: string | null
  market_liquidity: number | null
  // Market intelligence
  attractiveness_score: number | null
  attractiveness_reason: string | null
}

export interface SignalStats {
  total_signals_today: number
  high_suspicion_today: number
  active_markets: number
}

export interface Market {
  market_id: string
  question: string
  category: string | null
  volume_usd: number | null
  liquidity_usd: number | null
  active: boolean
  resolved: boolean
  end_date: string | null
  last_price: number | null
  attractiveness_score: number | null
  attractiveness_reason: string | null
  hours_to_resolution: number | null
  tier: 'hot' | 'scored' | 'unscored'
  signal_count: number
  last_signal_at: string | null
}

export interface MarketDetail {
  market_id: string
  question: string
  category: string | null
  volume_usd: number | null
  liquidity_usd: number | null
  active: boolean
  resolved: boolean
  resolved_price: number | null
  end_date: string | null
  last_price: number | null
  attractiveness_score: number | null
  attractiveness_reason: string | null
  hours_to_resolution: number | null
  tier: 'hot' | 'scored' | 'unscored'
}

export interface VolumePoint {
  hour: string
  trade_count: number
  volume_usd: number
  unique_wallets: number
}

export interface WalletProfile {
  wallet: string
  total_trades: number
  resolved_trades: number
  wins: number
  win_rate: number | null
  signal_count: number
  categories: CategoryBreakdown[]
}

export interface CategoryBreakdown {
  category: string
  trades: number
  volume_usd: number
}

export interface WalletTrade {
  trade_id: string
  market_id: string
  side: 'BUY' | 'SELL'
  price: number
  size_usd: number
  timestamp: string
  market_question: string | null
  category: string | null
  resolved: boolean
  resolved_price: number | null
}

export interface Budget {
  date: string
  market_scoring: BudgetTier
}

export interface BudgetTier {
  calls_used: number
  calls_limit: number
}

export interface HealthStatus {
  status: string
  timestamp: string
}

/* ── Metrics ─────────────────────────────────────────────────────── */

export interface TimeseriesPoint {
  bucket: string
  trades: number
  signals: number
  alerts: number
}

export interface MetricsOverview {
  funnel: {
    trades: number
    signals: number
    high_suspicion: number
  }
  score_distribution: Record<string, number>
  top_markets: TopMarket[]
  top_traded_markets: TopTradedMarket[]
  market_coverage: {
    total: number
    hot_eligible: number
    scored: number
    unscored: number
    avg_hot_score: number | null
    hot_capacity: number
  }
}

export interface AnomalyPoint {
  hour: string
  volume_usd: number
  trade_count: number
  z_score: number
}

export interface WalletLeader {
  wallet: string
  resolved_trades: number
  wins: number
  win_rate: number
  total_trades: number
  signal_count: number
  signal_hit_rate: number
}

export interface MarketAccuracy {
  market_id: string
  question: string | null
  category: string | null
  resolved_price: number | null
  signal_count: number
  high_score_count: number
  correct_high_score: number
  accuracy_pct: number | null
}

export interface HourPattern {
  hour: number
  trades: number
  signals: number
  high_suspicion: number
}

export interface TopMarket {
  market_id: string
  question: string | null
  category: string | null
  signal_count: number
  max_score: number | null
  avg_score: number | null
}

export interface TopTradedMarket {
  market_id: string
  question: string | null
  trade_count: number
  volume_usd: number
  unique_wallets: number
}

export interface IngestionMetrics {
  totals: { all_time: number; today: number }
  latest: { rest: string | null }
  markets_active_today: number
  wallets_active_today: number
  hourly: IngestionHourly[]
}

export interface IngestionHourly {
  bucket: string
  trades: number
}

export interface AccuracySummary {
  true_positives: number
  false_positives: number
  false_negatives: number
  true_negatives: number
  total_evaluated: number
  precision: number | null
  recall: number | null
  f1_score: number | null
}

export interface CalibrationBucket {
  score_bucket: string
  total: number
  correct: number
  accuracy_pct: number | null
  true_positives: number
}
