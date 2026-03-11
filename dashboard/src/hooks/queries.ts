/* ── Hooks for react-query data fetching ──────────────────────────── */

import { useQuery } from '@tanstack/react-query'
import {
  fetchSignals,
  fetchSignalStats,
  fetchMarketDetail,
  fetchMarketVolume,
  fetchMarketSignals,
  fetchWalletProfile,
  fetchWalletTrades,
  fetchWalletSignals,
  fetchBudget,
  fetchHealth,
  fetchTimeseries,
  fetchMetricsOverview,
  fetchIngestion,
} from '../api/client'

const POLL = 10_000 // 10s refresh

export function useSignals(opts?: { min_score?: number; market_id?: string; wallet?: string }) {
  return useQuery({
    queryKey: ['signals', opts],
    queryFn: () => fetchSignals(opts),
    refetchInterval: POLL,
  })
}

export function useSignalStats() {
  return useQuery({
    queryKey: ['signal-stats'],
    queryFn: fetchSignalStats,
    refetchInterval: POLL,
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

export function useWalletProfile(address: string) {
  return useQuery({
    queryKey: ['wallet', address],
    queryFn: () => fetchWalletProfile(address),
    enabled: !!address,
  })
}

export function useWalletTrades(address: string) {
  return useQuery({
    queryKey: ['wallet-trades', address],
    queryFn: () => fetchWalletTrades(address),
    enabled: !!address,
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

export function useIngestion() {
  return useQuery({
    queryKey: ['ingestion'],
    queryFn: fetchIngestion,
    refetchInterval: POLL,
  })
}
