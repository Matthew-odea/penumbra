import type { SignalStats } from '../api/types'
import type { Budget } from '../api/types'
import { fmtNum } from '../lib/format'

interface Props {
  stats: SignalStats | undefined
  budget: Budget | undefined
}

export default function SummaryCards({ stats, budget }: Props) {
  const ms = budget?.market_scoring
  const cards = [
    {
      label: 'Signals Today',
      value: fmtNum(stats?.total_signals_today),
      sub: null,
    },
    {
      label: 'High Suspicion',
      value: fmtNum(stats?.high_suspicion_today),
      sub: '≥ 80 score',
      accent: (stats?.high_suspicion_today ?? 0) > 0,
    },
    {
      label: 'Active Markets',
      value: fmtNum(stats?.active_markets),
      sub: null,
    },
    {
      label: 'Scoring Budget',
      value: ms ? `${ms.calls_used}/${ms.calls_limit}` : '—',
      sub: ms ? `${Math.round((ms.calls_used / ms.calls_limit) * 100)}% used` : null,
      warn: ms ? ms.calls_used / ms.calls_limit > 0.8 : false,
    },
  ]

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
      {cards.map((c) => (
        <div
          key={c.label}
          className="bg-surface-1 border border-border-subtle rounded-sm px-4 py-3"
        >
          <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-1">
            {c.label}
          </div>
          <div
            className={`font-mono text-lg font-medium ${
              c.accent ? 'text-red-400' : c.warn ? 'text-amber-400' : 'text-neutral-100'
            }`}
          >
            {c.value}
          </div>
          {c.sub && (
            <div className="text-[11px] text-neutral-600 mt-0.5">{c.sub}</div>
          )}
        </div>
      ))}
    </div>
  )
}
