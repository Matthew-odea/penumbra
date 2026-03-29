import { useState, useEffect } from 'react'
import { useSignals, useSignalStats, useBudget } from '../hooks/queries'
import SummaryCards from '../components/SummaryCards'
import SignalTable from '../components/SignalTable'

const SCORE_FILTERS = [
  { label: 'All', value: 0 },
  { label: '\u2265 40', value: 40 },
  { label: '\u2265 60', value: 60 },
  { label: '\u2265 80', value: 80 },
]

const TIME_RANGES = [
  { label: '1h', hours: 1 },
  { label: '6h', hours: 6 },
  { label: '24h', hours: 24 },
  { label: '7d', hours: 168 },
  { label: 'All', hours: undefined as number | undefined },
]

export default function Feed() {
  const [minScore, setMinScore] = useState(0)
  const [hours, setHours] = useState<number | undefined>(undefined)
  const [searchInput, setSearchInput] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')

  // Debounce search input by 300ms
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearch(searchInput)
    }, 300)
    return () => clearTimeout(timer)
  }, [searchInput])

  const { data: signals = [], isLoading, isError } = useSignals({
    min_score: minScore || undefined,
    hours,
    search: debouncedSearch || undefined,
  })
  const { data: stats } = useSignalStats()
  const { data: budget } = useBudget()

  return (
    <div className="px-5 py-4 space-y-4 max-w-[1600px] mx-auto">
      {/* Summary Cards */}
      <SummaryCards stats={stats} budget={budget} />

      {/* Filter Bar */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-4">
          {/* Score filter */}
          <div className="flex items-center gap-1">
            <span className="text-xs text-neutral-500 mr-2">Suspicion</span>
            {SCORE_FILTERS.map((f) => (
              <button
                key={f.value}
                onClick={() => setMinScore(f.value)}
                className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${
                  minScore === f.value
                    ? 'bg-surface-3 text-neutral-100 font-medium'
                    : 'text-neutral-500 hover:text-neutral-300 hover:bg-surface-2'
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>

          {/* Time range filter */}
          <div className="flex items-center gap-1">
            <span className="text-xs text-neutral-500 mr-2">Range</span>
            {TIME_RANGES.map((t) => (
              <button
                key={t.label}
                onClick={() => setHours(t.hours)}
                className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${
                  hours === t.hours
                    ? 'bg-surface-3 text-neutral-100 font-medium'
                    : 'text-neutral-500 hover:text-neutral-300 hover:bg-surface-2'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>

          {/* Search input */}
          <div className="flex items-center gap-1">
            <input
              type="text"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="Search market..."
              className="px-2.5 py-1 text-xs rounded-sm bg-surface-2 border border-border-subtle text-neutral-300 placeholder-neutral-600 outline-none focus:border-neutral-500 transition-colors w-48"
            />
          </div>
        </div>

        <div className="text-xs text-neutral-600">
          {signals.length} signal{signals.length !== 1 ? 's' : ''}
          <span className="text-neutral-700 ml-2">· Auto-refresh 10s</span>
        </div>
      </div>

      {/* Signal Table */}
      <div className="bg-surface-1 border border-border-subtle rounded-sm">
        {isLoading ? (
          <div className="py-16 text-center text-neutral-600 text-sm">Loading signals...</div>
        ) : isError ? (
          <div className="py-16 text-center text-red-400 text-sm">
            Failed to load signals. Is the API running?
          </div>
        ) : (
          <SignalTable signals={signals} />
        )}
      </div>
    </div>
  )
}
