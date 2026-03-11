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
  // Reasoning (may be null if Judge hasn't processed yet)
  classification: 'INFORMED' | 'NOISE' | null
  tier1_confidence: number | null
  suspicion_score: number | null
  reasoning: string | null
  key_evidence: string | null
  tier1_model: string | null
  tier2_model: string | null
  // Market metadata
  market_question: string | null
  category: string | null
  market_liquidity: number | null
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
  end_date: string | null
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
  tier1: BudgetTier
  tier2: BudgetTier
}

export interface BudgetTier {
  calls_used: number
  calls_limit: number
}

export interface HealthStatus {
  status: string
  timestamp: string
}
