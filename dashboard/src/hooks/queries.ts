/* ── Hooks for react-query data fetching ──────────────────────────── */

import { useQuery } from '@tanstack/react-query'
import {
  fetchSignals,
  fetchSignalDetail,
  fetchSignalStats,
  fetchMarkets,
  fetchMarketDetail,
  fetchMarketVolume,
  fetchMarketSignals,
  fetchMarketAnomalies,
  fetchMarketVPIN,
  fetchMarketLambda,
  fetchWallets,
  fetchWalletProfile,
  fetchWalletTrades,
  fetchWalletSignals,
  fetchBudget,
  fetchHealth,
  fetchTimeseries,
  fetchMetricsOverview,
  fetchMetricsAccuracy,
  fetchAccuracySummary,
  fetchAccuracyCalibration,
  fetchMetricsPatterns,
  fetchIngestion,
} from '../api/client'

const POLL = 10_000 // 10s refresh

export function useSignals(opts?: {
  min_score?: number
  market_id?: string
  wallet?: string
  hours?: number
  search?: string
}) {
  return useQuery({
    queryKey: ['signals', opts],
    queryFn: () => fetchSignals(opts),
    refetchInterval: POLL,
  })
}

export function useSignalDetail(signalId: string) {
  return useQuery({
    queryKey: ['signal', signalId],
    queryFn: () => fetchSignalDetail(signalId),
    enabled: !!signalId,
  })
}

export function useSignalStats() {
  return useQuery({
    queryKey: ['signal-stats'],
    queryFn: fetchSignalStats,
    refetchInterval: POLL,
  })
}

export function useAllMarkets(opts?: {
  tier?: 'hot' | 'scored' | 'unscored'
  min_score?: number
  sort?: 'signals' | 'priority' | 'liquidity' | 'resolution'
}) {
  return useQuery({
    queryKey: ['all-markets', opts],
    queryFn: () => fetchMarkets({ limit: 2000, active_only: false, ...opts }),
    refetchInterval: 60_000,
  })
}

export function useMarketDetail(marketId: string) {
  return useQuery({
    queryKey: ['market', marketId],
    queryFn: () => fetchMarketDetail(marketId),
    enabled: !!marketId,
  })
}

export function useMarketVolume(marketId: string, hours = 24) {
  return useQuery({
    queryKey: ['market-volume', marketId, hours],
    queryFn: () => fetchMarketVolume(marketId, hours),
    enabled: !!marketId,
    refetchInterval: POLL,
  })
}

export function useMarketSignals(marketId: string) {
  return useQuery({
    queryKey: ['market-signals', marketId],
    queryFn: () => fetchMarketSignals(marketId),
    enabled: !!marketId,
    refetchInterval: POLL,
  })
}

export function useMarketAnomalies(marketId: string) {
  return useQuery({
    queryKey: ['market-anomalies', marketId],
    queryFn: () => fetchMarketAnomalies(marketId),
    enabled: !!marketId,
    refetchInterval: POLL,
  })
}

export function useMarketVPIN(marketId: string) {
  return useQuery({
    queryKey: ['market-vpin', marketId],
    queryFn: () => fetchMarketVPIN(marketId),
    enabled: !!marketId,
    refetchInterval: 30_000,
  })
}

export function useMarketLambda(marketId: string) {
  return useQuery({
    queryKey: ['market-lambda', marketId],
    queryFn: () => fetchMarketLambda(marketId),
    enabled: !!marketId,
    refetchInterval: 30_000,
  })
}

export function useWallets(limit = 50) {
  return useQuery({
    queryKey: ['wallets', limit],
    queryFn: () => fetchWallets(limit),
    refetchInterval: 60_000, // 1 min — leaderboard changes slowly
  })
}

export function useWalletProfile(address: string) {
  return useQuery({
    queryKey: ['wallet', address],
    queryFn: () => fetchWalletProfile(address),
    enabled: !!address,
    refetchInterval: 30_000,
  })
}

export function useWalletTrades(address: string) {
  return useQuery({
    queryKey: ['wallet-trades', address],
    queryFn: () => fetchWalletTrades(address),
    enabled: !!address,
    refetchInterval: 30_000,
  })
}

export function useWalletSignals(address: string) {
  return useQuery({
    queryKey: ['wallet-signals', address],
    queryFn: () => fetchWalletSignals(address),
    enabled: !!address,
    refetchInterval: POLL,
  })
}

export function useBudget() {
  return useQuery({
    queryKey: ['budget'],
    queryFn: fetchBudget,
    refetchInterval: 30_000, // 30s — budget changes slowly
  })
}

export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    refetchInterval: 15_000,
  })
}

export function useTimeseries(hours = 6, bucketMinutes = 5) {
  return useQuery({
    queryKey: ['timeseries', hours, bucketMinutes],
    queryFn: () => fetchTimeseries(hours, bucketMinutes),
    refetchInterval: POLL,
  })
}

export function useMetricsOverview() {
  return useQuery({
    queryKey: ['metrics-overview'],
    queryFn: fetchMetricsOverview,
    refetchInterval: POLL,
  })
}

export function useMetricsAccuracy() {
  return useQuery({
    queryKey: ['metrics-accuracy'],
    queryFn: fetchMetricsAccuracy,
    refetchInterval: 60_000,
  })
}

export function useAccuracySummary() {
  return useQuery({
    queryKey: ['accuracy-summary'],
    queryFn: fetchAccuracySummary,
    refetchInterval: 60_000,
  })
}

export function useAccuracyCalibration() {
  return useQuery({
    queryKey: ['accuracy-calibration'],
    queryFn: fetchAccuracyCalibration,
    refetchInterval: 60_000,
  })
}

export function useMetricsPatterns() {
  return useQuery({
    queryKey: ['metrics-patterns'],
    queryFn: fetchMetricsPatterns,
    refetchInterval: 60_000,
  })
}

export function useIngestion() {
  return useQuery({
    queryKey: ['ingestion'],
    queryFn: fetchIngestion,
    refetchInterval: POLL,
  })
}
