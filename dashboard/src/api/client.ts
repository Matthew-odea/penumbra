/* ── API client — fetch wrapper with query string helpers ─────────── */

import type {
  Signal,
  SignalStats,
  Market,
  MarketDetail,
  VolumePoint,
  WalletProfile,
  WalletTrade,
  Budget,
  HealthStatus,
  TimeseriesPoint,
  MetricsOverview,
} from './types'

const BASE = '/api'

async function get<T>(path: string, params?: Record<string, string | number | boolean>): Promise<T> {
  const url = new URL(path, window.location.origin)
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') {
        url.searchParams.set(k, String(v))
      }
    })
  }
  const res = await fetch(url.toString())
  if (!res.ok) {
    throw new Error(`API ${res.status}: ${res.statusText}`)
  }
  return res.json()
}

/* ── Signals ─────────────────────────────────────────────────────────── */

export function fetchSignals(opts?: {
  limit?: number
  min_score?: number
  market_id?: string
  wallet?: string
}): Promise<Signal[]> {
  return get<Signal[]>(`${BASE}/signals`, opts)
}

export function fetchSignalStats(): Promise<SignalStats> {
  return get<SignalStats>(`${BASE}/signals/stats`)
}

/* ── Markets ─────────────────────────────────────────────────────────── */

export function fetchMarkets(opts?: {
  limit?: number
  active_only?: boolean
}): Promise<Market[]> {
  return get<Market[]>(`${BASE}/markets`, opts)
}

export function fetchMarketDetail(marketId: string): Promise<MarketDetail> {
  return get<MarketDetail>(`${BASE}/markets/${marketId}`)
}

export function fetchMarketVolume(
  marketId: string,
  hours = 24,
): Promise<VolumePoint[]> {
  return get<VolumePoint[]>(`${BASE}/markets/${marketId}/volume`, { hours })
}

export function fetchMarketSignals(
  marketId: string,
  limit = 50,
): Promise<Signal[]> {
  return get<Signal[]>(`${BASE}/markets/${marketId}/signals`, { limit })
}

/* ── Wallets ─────────────────────────────────────────────────────────── */

export function fetchWalletProfile(address: string): Promise<WalletProfile> {
  return get<WalletProfile>(`${BASE}/wallets/${address}`)
}

export function fetchWalletTrades(
  address: string,
  limit = 100,
): Promise<WalletTrade[]> {
  return get<WalletTrade[]>(`${BASE}/wallets/${address}/trades`, { limit })
}

export function fetchWalletSignals(
  address: string,
  limit = 50,
): Promise<Signal[]> {
  return get<Signal[]>(`${BASE}/wallets/${address}/signals`, { limit })
}

/* ── Budget ──────────────────────────────────────────────────────────── */

export function fetchBudget(): Promise<Budget> {
  return get<Budget>(`${BASE}/budget`)
}

/* ── Health ──────────────────────────────────────────────────────────── */

export function fetchHealth(): Promise<HealthStatus> {
  return get<HealthStatus>(`${BASE}/health`)
}

/* ── Metrics ─────────────────────────────────────────────────────────── */

export function fetchTimeseries(
  hours = 6,
  bucket_minutes = 5,
): Promise<TimeseriesPoint[]> {
  return get<TimeseriesPoint[]>(`${BASE}/metrics/timeseries`, { hours, bucket_minutes })
}

export function fetchMetricsOverview(): Promise<MetricsOverview> {
  return get<MetricsOverview>(`${BASE}/metrics/overview`)
}
