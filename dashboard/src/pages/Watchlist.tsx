import { useNavigate } from 'react-router-dom'
import { useWatchlist, useMetricsOverview } from '../hooks/queries'
import { fmtUsd } from '../lib/format'
import type { WatchlistMarket } from '../api/types'

function fmtResolution(hours: number | null): string {
  if (hours === null) return '—'
  if (hours < 1) return '< 1h'
  if (hours < 24) return `${hours}h`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days}d`
  const months = Math.round(days / 30)
  return `${months}mo`
}

function urgencyClass(hours: number | null): string {
  if (hours === null) return 'bg-neutral-800'
  if (hours < 24) return 'bg-red-500'
  if (hours < 72) return 'bg-amber-500'
  if (hours < 168) return 'bg-emerald-600'
  return 'bg-neutral-700'
}

function scoreBadge(score: number): string {
  if (score >= 80) return 'bg-red-500/20 text-red-300'
  if (score >= 60) return 'bg-amber-500/20 text-amber-300'
  if (score >= 40) return 'bg-neutral-700 text-neutral-300'
  return 'bg-neutral-800 text-neutral-500'
}

function PriceBar({ price }: { price: number | null }) {
  if (price === null) return <span className="text-neutral-600">—</span>
  const pct = Math.round(price * 100)
  const barWidth = pct
  const color = pct > 65 ? 'bg-emerald-500' : pct < 35 ? 'bg-red-500' : 'bg-blue-500'
  return (
    <div className="flex items-center gap-2 min-w-[80px]">
      <div className="flex-1 h-1.5 bg-surface-3 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${barWidth}%` }} />
      </div>
      <span className="font-mono text-neutral-300 text-[11px] w-8 text-right">{pct}%</span>
    </div>
  )
}

function WatchlistRow({ market }: { market: WatchlistMarket }) {
  const navigate = useNavigate()
  const hours = market.hours_to_resolution

  return (
    <tr
      className="border-b border-border-subtle hover:bg-surface-2 transition-colors cursor-pointer group"
      onClick={() => navigate(`/market/${market.market_id}`)}
    >
      {/* Urgency stripe */}
      <td className="py-0 w-1 pr-0">
        <div className={`w-1 h-full min-h-[40px] rounded-sm ${urgencyClass(hours)}`} />
      </td>

      {/* Market question + attractiveness reason */}
      <td className="py-3 px-4">
        <div className="text-neutral-200 text-xs leading-snug max-w-[480px]">
          {market.question}
        </div>
        {market.attractiveness_reason && (
          <div className="text-[10px] text-neutral-600 mt-0.5 max-w-[480px] truncate">
            {market.attractiveness_reason}
          </div>
        )}
      </td>

      {/* Attractiveness score */}
      <td className="py-3 px-3 whitespace-nowrap">
        <span className={`inline-block px-2 py-0.5 rounded-sm font-mono font-medium text-[11px] ${scoreBadge(market.attractiveness_score)}`}>
          {market.attractiveness_score}
        </span>
      </td>

      {/* Time to resolution */}
      <td className="py-3 px-3 whitespace-nowrap">
        <span className={`font-mono text-xs ${
          hours !== null && hours < 24 ? 'text-red-400' :
          hours !== null && hours < 72 ? 'text-amber-400' :
          'text-neutral-400'
        }`}>
          {fmtResolution(hours)}
        </span>
      </td>

      {/* Current price / probability */}
      <td className="py-3 px-3">
        <PriceBar price={market.last_price} />
      </td>

      {/* Liquidity */}
      <td className="py-3 px-3 text-right font-mono text-xs text-neutral-500 whitespace-nowrap">
        {market.liquidity_usd != null ? fmtUsd(market.liquidity_usd) : '—'}
      </td>

      {/* Priority score */}
      <td className="py-3 px-3 text-right font-mono text-xs whitespace-nowrap">
        <span className="text-neutral-500">{market.priority_score.toFixed(2)}</span>
      </td>

      {/* Signals today */}
      <td className="py-3 px-3 text-right font-mono text-xs whitespace-nowrap">
        <span className={market.signals_today > 0 ? 'text-amber-400' : 'text-neutral-600'}>
          {market.signals_today}
        </span>
      </td>
    </tr>
  )
}

export default function Watchlist() {
  const { data: markets = [], isLoading, dataUpdatedAt } = useWatchlist()
  const { data: overview } = useMetricsOverview()
  const coverage = overview?.market_coverage

  const secondsAgo = dataUpdatedAt
    ? Math.round((Date.now() - dataUpdatedAt) / 1000)
    : null

  return (
    <div className="px-5 py-4 space-y-4 max-w-[1600px] mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-base font-semibold text-neutral-100">Watchlist</h1>
          <p className="text-xs text-neutral-600 mt-0.5">
            Markets actively monitored for informed trading activity
          </p>
        </div>
        <div className="flex items-center gap-6 text-xs text-neutral-500">
          {coverage && (
            <div className="flex items-center gap-4">
              <span>
                <span className="text-neutral-300 font-mono">{markets.length}</span>
                <span className="text-neutral-600"> / {coverage.hot_capacity} hot</span>
              </span>
              <span>
                <span className="text-neutral-300 font-mono">{coverage.scored}</span>
                <span className="text-neutral-600"> scored</span>
              </span>
              {coverage.unscored > 0 && (
                <span className="text-amber-600">
                  {coverage.unscored} scoring…
                </span>
              )}
            </div>
          )}
          {secondsAgo !== null && (
            <span className="text-neutral-700">
              Updated {secondsAgo}s ago
            </span>
          )}
        </div>
      </div>

      {/* Legend */}
      <div className="flex items-center gap-4 text-[10px] text-neutral-600">
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-sm bg-red-500" />
          <span>&lt; 24h</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-sm bg-amber-500" />
          <span>24–72h</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-sm bg-emerald-600" />
          <span>3–7d</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-2 rounded-sm bg-neutral-700" />
          <span>&gt; 7d</span>
        </div>
        <span className="text-neutral-700 ml-2">Left bar = time urgency · Score = LLM insider-tradability (0-100)</span>
      </div>

      {/* Table */}
      <div className="bg-surface-1 border border-border-subtle rounded-sm">
        {isLoading ? (
          <div className="py-16 text-center text-neutral-600 text-sm">Loading watchlist…</div>
        ) : markets.length === 0 ? (
          <div className="py-16 text-center space-y-2">
            <div className="text-neutral-500 text-sm">No hot-tier markets yet</div>
            <div className="text-neutral-700 text-xs">
              LLM scoring is in progress. Markets will appear once scored above the attractiveness threshold.
            </div>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border-subtle text-neutral-500 text-left">
                  <th className="py-2 w-1" />
                  <th className="py-2 px-4 font-medium">Market</th>
                  <th className="py-2 px-3 font-medium">Score</th>
                  <th className="py-2 px-3 font-medium">Resolves</th>
                  <th className="py-2 px-3 font-medium">Probability</th>
                  <th className="py-2 px-3 font-medium text-right">Liquidity</th>
                  <th className="py-2 px-3 font-medium text-right">Priority</th>
                  <th className="py-2 px-3 font-medium text-right">Signals</th>
                </tr>
              </thead>
              <tbody>
                {markets.map((m) => (
                  <WatchlistRow key={m.market_id} market={m} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
