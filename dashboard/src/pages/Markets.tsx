import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAllMarkets } from '../hooks/queries'
import { fmtUsd } from '../lib/format'
import type { Market } from '../api/types'

type Tab = 'hot' | 'all' | 'unscored'

const TABS: { id: Tab; label: string }[] = [
  { id: 'hot', label: 'Hot Tier' },
  { id: 'all', label: 'All Scored' },
  { id: 'unscored', label: 'Pending Score' },
]

function fmtResolution(hours: number | null): string {
  if (hours === null) return '—'
  if (hours < 1) return '< 1h'
  if (hours < 24) return `${hours}h`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days}d`
  return `${Math.round(days / 30)}mo`
}

function scoreBadge(score: number | null): string {
  if (score === null) return 'bg-neutral-800 text-neutral-600'
  if (score >= 80) return 'bg-red-500/20 text-red-300'
  if (score >= 60) return 'bg-amber-500/20 text-amber-300'
  if (score >= 40) return 'bg-neutral-700 text-neutral-300'
  return 'bg-neutral-800 text-neutral-500'
}

function TierBadge({ tier }: { tier: Market['tier'] }) {
  if (tier === 'hot') return (
    <span className="inline-block px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide rounded-sm bg-red-500/15 text-red-400 border border-red-500/20">
      HOT
    </span>
  )
  if (tier === 'scored') return (
    <span className="inline-block px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide rounded-sm bg-surface-3 text-neutral-500">
      scored
    </span>
  )
  return (
    <span className="inline-block px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide rounded-sm bg-surface-2 text-neutral-600 border border-border-subtle">
      pending
    </span>
  )
}

function MarketRow({ market }: { market: Market }) {
  const navigate = useNavigate()
  return (
    <tr
      className="border-b border-border-subtle hover:bg-surface-2 transition-colors cursor-pointer"
      onClick={() => navigate(`/market/${market.market_id}`)}
    >
      <td className="py-2.5 px-4 max-w-[400px]">
        <div className="text-neutral-200 text-xs leading-snug truncate">{market.question}</div>
        {market.attractiveness_reason && (
          <div className="text-[10px] text-neutral-600 mt-0.5 truncate">{market.attractiveness_reason}</div>
        )}
      </td>
      <td className="py-2.5 px-3">
        <TierBadge tier={market.tier} />
      </td>
      <td className="py-2.5 px-3">
        {market.attractiveness_score !== null ? (
          <span className={`inline-block px-2 py-0.5 rounded-sm font-mono font-medium text-[11px] ${scoreBadge(market.attractiveness_score)}`}>
            {market.attractiveness_score}
          </span>
        ) : (
          <span className="text-neutral-600 text-[11px]">—</span>
        )}
      </td>
      <td className="py-2.5 px-3 font-mono text-xs whitespace-nowrap">
        <span className={
          market.hours_to_resolution !== null && market.hours_to_resolution < 24 ? 'text-red-400' :
          market.hours_to_resolution !== null && market.hours_to_resolution < 72 ? 'text-amber-400' :
          'text-neutral-500'
        }>
          {fmtResolution(market.hours_to_resolution)}
        </span>
      </td>
      <td className="py-2.5 px-3 font-mono text-xs text-neutral-500 whitespace-nowrap">
        {market.last_price !== null ? `${Math.round(market.last_price * 100)}%` : '—'}
      </td>
      <td className="py-2.5 px-3 text-right font-mono text-xs text-neutral-500 whitespace-nowrap">
        {market.liquidity_usd != null ? fmtUsd(market.liquidity_usd) : '—'}
      </td>
      <td className="py-2.5 px-3 text-right font-mono text-xs whitespace-nowrap">
        <span className={market.signal_count > 0 ? 'text-amber-400' : 'text-neutral-600'}>
          {market.signal_count}
        </span>
      </td>
      <td className="py-2.5 px-3 text-xs text-neutral-600 max-w-[100px] truncate">
        {market.category || '—'}
      </td>
    </tr>
  )
}

export default function Markets() {
  const [tab, setTab] = useState<Tab>('hot')
  const [search, setSearch] = useState('')

  const sort = tab === 'hot' ? 'priority' : tab === 'all' ? 'priority' : 'liquidity'
  const { data: markets = [], isLoading } = useAllMarkets({ sort: sort as any })

  const filtered = markets.filter((m) => {
    if (tab === 'hot' && m.tier !== 'hot') return false
    if (tab === 'all' && m.tier === 'unscored') return false
    if (tab === 'unscored' && m.tier !== 'unscored') return false
    if (search && !m.question?.toLowerCase().includes(search.toLowerCase())) return false
    return true
  })

  const counts = {
    hot: markets.filter((m) => m.tier === 'hot').length,
    all: markets.filter((m) => m.tier !== 'unscored').length,
    unscored: markets.filter((m) => m.tier === 'unscored').length,
  }

  return (
    <div className="px-5 py-4 space-y-4 max-w-[1600px] mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-base font-semibold text-neutral-100">Markets</h1>
          <p className="text-xs text-neutral-600 mt-0.5">
            All Polymarket markets discovered — scored for insider-trading attractiveness
          </p>
        </div>
        <div className="text-xs text-neutral-700">
          {markets.length.toLocaleString()} total markets
        </div>
      </div>

      {/* Tabs + Search */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-3 py-1.5 text-xs rounded-sm transition-colors flex items-center gap-1.5 ${
                tab === t.id
                  ? 'bg-surface-3 text-neutral-100 font-medium'
                  : 'text-neutral-500 hover:text-neutral-300 hover:bg-surface-2'
              }`}
            >
              {t.label}
              <span className="font-mono text-[10px] text-neutral-600">
                {counts[t.id]}
              </span>
            </button>
          ))}
        </div>
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search markets…"
          className="bg-surface-2 border border-border-subtle rounded-sm px-3 py-1.5 text-xs text-neutral-300 placeholder-neutral-600 focus:outline-none focus:border-neutral-500 w-56"
        />
      </div>

      {/* Table */}
      <div className="bg-surface-1 border border-border-subtle rounded-sm">
        {isLoading ? (
          <div className="py-16 text-center text-neutral-600 text-sm">Loading markets…</div>
        ) : filtered.length === 0 ? (
          <div className="py-16 text-center text-neutral-600 text-sm">
            {search ? `No markets matching "${search}"` : 'No markets in this category.'}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border-subtle text-neutral-500 text-left">
                  <th className="py-2 px-4 font-medium">Market</th>
                  <th className="py-2 px-3 font-medium">Tier</th>
                  <th className="py-2 px-3 font-medium">Score</th>
                  <th className="py-2 px-3 font-medium">Resolves</th>
                  <th className="py-2 px-3 font-medium">Price</th>
                  <th className="py-2 px-3 font-medium text-right">Liquidity</th>
                  <th className="py-2 px-3 font-medium text-right">Signals</th>
                  <th className="py-2 px-3 font-medium">Tags</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((m) => (
                  <MarketRow key={m.market_id} market={m} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
