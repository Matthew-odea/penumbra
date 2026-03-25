import { useParams, Link } from 'react-router-dom'
import { useMarketDetail, useMarketVolume, useMarketSignals, useMarketAnomalies } from '../hooks/queries'
import { fmtUsd } from '../lib/format'
import VolumeChart from '../components/VolumeChart'
import SignalTable from '../components/SignalTable'

export default function MarketView() {
  const { marketId } = useParams<{ marketId: string }>()
  const { data: market, isLoading: loadingMarket } = useMarketDetail(marketId!)
  const { data: volume = [] } = useMarketVolume(marketId!)
  const { data: anomalies = [] } = useMarketAnomalies(marketId!)
  const { data: signals = [] } = useMarketSignals(marketId!)

  if (loadingMarket) {
    return (
      <div className="px-5 py-4">
        <div className="text-neutral-600 text-sm">Loading market…</div>
      </div>
    )
  }

  if (!market) {
    return (
      <div className="px-5 py-4">
        <div className="text-red-400 text-sm">Market not found.</div>
      </div>
    )
  }

  return (
    <div className="px-5 py-4 space-y-4 max-w-[1600px] mx-auto">
      {/* Breadcrumb */}
      <div className="flex items-center gap-2 text-xs text-neutral-500">
        <Link to="/" className="hover:text-neutral-300 transition-colors">Feed</Link>
        <span className="text-neutral-700">›</span>
        <span className="text-neutral-400">Market</span>
      </div>

      {/* Header */}
      <div className="space-y-2">
        <h1 className="text-base font-semibold text-neutral-100 leading-tight">
          {market.question}
        </h1>
        <div className="flex items-center gap-4 text-xs text-neutral-500">
          {market.category && (
            <span className="px-2 py-0.5 bg-surface-2 rounded-sm text-neutral-400">
              {market.category}
            </span>
          )}
          <span>Volume: <span className="text-neutral-300 font-mono">{fmtUsd(market.volume_usd)}</span></span>
          <span>Liquidity: <span className="text-neutral-300 font-mono">{fmtUsd(market.liquidity_usd)}</span></span>
          {market.resolved && (
            <span className="text-amber-400">
              Resolved @ {market.resolved_price?.toFixed(2)}
            </span>
          )}
          {!market.active && !market.resolved && (
            <span className="text-neutral-600">Inactive</span>
          )}
        </div>
      </div>

      {/* Volume Chart */}
      <div className="bg-surface-1 border border-border-subtle rounded-sm p-4">
        <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-3">
          Hourly Volume (24h)
        </div>
        <VolumeChart data={volume} anomalies={anomalies} />
        {anomalies.length > 0 && (
          <div className="text-[10px] text-neutral-600 mt-1 flex items-center gap-1">
            <span className="inline-block w-3 h-px bg-red-500" />
            Red line = volume anomaly Z-score (right axis). Spikes indicate statistically unusual activity.
          </div>
        )}
      </div>

      {/* Market Signals */}
      <div className="bg-surface-1 border border-border-subtle rounded-sm">
        <div className="px-4 py-3 border-b border-border-subtle">
          <span className="text-[11px] uppercase tracking-wider text-neutral-500">
            Signals ({signals.length})
          </span>
        </div>
        <SignalTable signals={signals} />
      </div>
    </div>
  )
}
