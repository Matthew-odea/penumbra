import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  LineChart,
  Line,
  BarChart,
  Bar,
  AreaChart,
  Area,
  Cell,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Legend,
} from 'recharts'
import {
  useTimeseries,
  useMetricsOverview,
  useBudget,
  useIngestion,
  useMetricsAccuracy,
  useMetricsPatterns,
} from '../hooks/queries'
import { fmtNum } from '../lib/format'

const RANGE_OPTIONS = [
  { label: '1h', hours: 1, bucket: 1 },
  { label: '6h', hours: 6, bucket: 5 },
  { label: '24h', hours: 24, bucket: 15 },
  { label: '3d', hours: 72, bucket: 60 },
]

export default function Metrics() {
  const [rangeIdx, setRangeIdx] = useState(1) // default 6h
  const range = RANGE_OPTIONS[rangeIdx]

  const { data: timeseries = [], isLoading: tsLoading } = useTimeseries(range.hours, range.bucket)
  const { data: overview } = useMetricsOverview()
  const { data: budget } = useBudget()
  const { data: ingestion } = useIngestion()
  const { data: accuracy = [] } = useMetricsAccuracy()
  const { data: patterns = [] } = useMetricsPatterns()
  const navigate = useNavigate()

  const chartData = timeseries.map((p) => ({
    ...p,
    label: new Date(p.bucket).toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    }),
  }))

  const funnel = overview?.funnel
  const classification = overview?.classification ?? {}
  const scoreDist = overview?.score_distribution ?? {}
  const topMarkets = overview?.top_markets ?? []
  const t2cov = overview?.tier2_coverage ?? { real: 0, fallback: 0, total: 0 }

  const t1Used = budget?.tier1.calls_used ?? 0
  const t1Limit = budget?.tier1.calls_limit ?? 1
  const t1Pct = Math.min(100, Math.round((t1Used / t1Limit) * 100))

  const t2CovPct = t2cov.total > 0 ? Math.round((t2cov.real / t2cov.total) * 100) : null

  return (
    <div className="px-5 py-4 space-y-5 max-w-[1600px] mx-auto">
      {/* ── Header + Range Selector ──────────────────────────────── */}
      <div className="flex items-center justify-between">
        <h1 className="text-base font-semibold text-neutral-100">Pipeline Metrics</h1>
        <div className="flex items-center gap-1">
          {RANGE_OPTIONS.map((opt, i) => (
            <button
              key={opt.label}
              onClick={() => setRangeIdx(i)}
              className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${
                rangeIdx === i
                  ? 'bg-surface-3 text-neutral-100 font-medium'
                  : 'text-neutral-500 hover:text-neutral-300 hover:bg-surface-2'
              }`}
            >
              {opt.label}
            </button>
          ))}
          <span className="text-xs text-neutral-700 ml-2">· Auto-refresh 10s</span>
        </div>
      </div>

      {/* ── Row 1: Pipeline Activity Line Chart ──────────────────── */}
      <div className="bg-surface-1 border border-border-subtle rounded-sm p-4">
        <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-3">
          Pipeline Activity
        </div>
        {tsLoading ? (
          <div className="h-56 flex items-center justify-center text-neutral-600 text-sm">
            Loading…
          </div>
        ) : chartData.length === 0 ? (
          <div className="h-56 flex items-center justify-center text-neutral-600 text-sm">
            No activity data for this period.
          </div>
        ) : (
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData} margin={{ top: 4, right: 48, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e1e1e" vertical={false} />
                <XAxis
                  dataKey="label"
                  tick={{ fill: '#525252', fontSize: 10 }}
                  axisLine={{ stroke: '#1e1e1e' }}
                  tickLine={false}
                  interval="preserveStartEnd"
                />
                {/* Left axis: trade volume */}
                <YAxis
                  yAxisId="trades"
                  tick={{ fill: '#404040', fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                />
                {/* Right axis: signals / llm / alerts */}
                <YAxis
                  yAxisId="events"
                  orientation="right"
                  tick={{ fill: '#404040', fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                  allowDecimals={false}
                />
                <Tooltip
                  contentStyle={{
                    background: '#191919',
                    border: '1px solid #2a2a2a',
                    borderRadius: '3px',
                    fontSize: '11px',
                    color: '#d4d4d4',
                  }}
                  labelStyle={{ color: '#737373' }}
                />
                <Legend
                  wrapperStyle={{ fontSize: '11px', color: '#737373' }}
                  iconType="plainline"
                />
                <Line yAxisId="trades" type="monotone" dataKey="trades" stroke="#525252" strokeWidth={1.5} dot={false} name="Trades" />
                <Line yAxisId="events" type="monotone" dataKey="signals" stroke="#f59e0b" strokeWidth={1.5} dot={false} name="Signals" />
                <Line yAxisId="events" type="monotone" dataKey="llm_t1" stroke="#3b82f6" strokeWidth={1.5} dot={false} name="LLM T1" />
                <Line yAxisId="events" type="monotone" dataKey="llm_t2" stroke="#8b5cf6" strokeWidth={1.5} dot={false} name="LLM T2" strokeDasharray="4 2" />
                <Line yAxisId="events" type="monotone" dataKey="alerts" stroke="#ef4444" strokeWidth={2} dot={false} name="Alerts (≥80)" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>

      {/* ── Row 1b: Ingestion Source Breakdown ────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <IngestionCard
          label="Trades (Today)"
          value={ingestion?.totals?.today}
          sub={ingestion?.totals ? `${fmtNum(ingestion.totals.all_time)} all-time` : undefined}
          accent
        />
        <IngestionCard
          label="Active Markets"
          value={ingestion?.markets_active_today}
          sub={(() => {
            const rest = ingestion?.latest?.rest
            if (!rest) return undefined
            return `Last trade ${new Date(rest).toLocaleTimeString('en-US', {
              hour: 'numeric', minute: '2-digit', hour12: true,
            })}`
          })()}
        />
        <IngestionCard
          label="Unique Wallets (Today)"
          value={ingestion?.wallets_active_today}
          sub={undefined}
        />
      </div>

      {/* Ingestion source area chart (24h) */}
      {(ingestion?.hourly?.length ?? 0) > 0 && (
        <div className="bg-surface-1 border border-border-subtle rounded-sm p-4">
          <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-3">
            Trades Ingested (24h)
          </div>
          <div className="h-40">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart
                data={(ingestion?.hourly ?? []).map((p) => ({
                  ...p,
                  label: new Date(p.bucket).toLocaleTimeString('en-US', {
                    hour: 'numeric',
                    minute: '2-digit',
                    hour12: true,
                  }),
                }))}
                margin={{ top: 4, right: 16, bottom: 0, left: 0 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#1e1e1e" vertical={false} />
                <XAxis
                  dataKey="label"
                  tick={{ fill: '#525252', fontSize: 10 }}
                  axisLine={{ stroke: '#1e1e1e' }}
                  tickLine={false}
                  interval="preserveStartEnd"
                />
                <YAxis
                  tick={{ fill: '#525252', fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                  allowDecimals={false}
                />
                <Tooltip
                  contentStyle={{
                    background: '#191919',
                    border: '1px solid #2a2a2a',
                    borderRadius: '3px',
                    fontSize: '11px',
                    color: '#d4d4d4',
                  }}
                  labelStyle={{ color: '#737373' }}
                />
                <Area type="monotone" dataKey="trades" stroke="#3b82f6" fill="#1e3a5f" name="Trades" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* ── Row 2: Funnel + Score Distribution + Classification ─── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Detection Funnel */}
        <div className="bg-surface-1 border border-border-subtle rounded-sm p-4">
          <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-4">
            Detection Funnel (Today)
          </div>
          <div className="space-y-3">
            <FunnelRow label="Trades" value={funnel?.trades} total={funnel?.trades} />
            <FunnelRow label="Signals" value={funnel?.signals} total={funnel?.trades} />
            <FunnelRow label="Classified" value={funnel?.classified} total={funnel?.trades} />
            <FunnelRow label="High Suspicion" value={funnel?.high_suspicion} total={funnel?.trades} accent />
          </div>
        </div>

        {/* Score Distribution */}
        <div className="bg-surface-1 border border-border-subtle rounded-sm p-4">
          <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-3">
            Score Distribution (Today)
          </div>
          <div className="h-44">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={Object.entries(scoreDist).map(([range, count]) => ({ range, count }))}
                margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#1e1e1e" vertical={false} />
                <XAxis
                  dataKey="range"
                  tick={{ fill: '#525252', fontSize: 10 }}
                  axisLine={{ stroke: '#1e1e1e' }}
                  tickLine={false}
                />
                <YAxis
                  tick={{ fill: '#525252', fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                  allowDecimals={false}
                />
                <Tooltip
                  contentStyle={{
                    background: '#191919',
                    border: '1px solid #2a2a2a',
                    borderRadius: '3px',
                    fontSize: '11px',
                    color: '#d4d4d4',
                  }}
                  formatter={(value: number) => [value, 'Signals']}
                />
                <Bar dataKey="count" radius={[2, 2, 0, 0]} maxBarSize={32}>
                  {Object.keys(scoreDist).map((range, i) => {
                    const colors = ['#404040', '#525252', '#f59e0b', '#f97316', '#ef4444']
                    return <Cell key={range} fill={colors[i] ?? '#525252'} />
                  })}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* Classification + Budget */}
        <div className="space-y-4">
          {/* Classification Breakdown */}
          <div className="bg-surface-1 border border-border-subtle rounded-sm p-4">
            <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-3">
              Classification (Today)
            </div>
            <div className="flex items-end gap-6">
              <div>
                <div className="font-mono text-2xl font-medium text-red-400">
                  {fmtNum(classification['INFORMED'] ?? 0)}
                </div>
                <div className="text-[11px] text-neutral-500 mt-0.5">INFORMED</div>
              </div>
              <div>
                <div className="font-mono text-2xl font-medium text-neutral-500">
                  {fmtNum(classification['NOISE'] ?? 0)}
                </div>
                <div className="text-[11px] text-neutral-500 mt-0.5">NOISE</div>
              </div>
              <div className="text-xs text-neutral-600 pb-1">
                {(() => {
                  const total = (classification['INFORMED'] ?? 0) + (classification['NOISE'] ?? 0)
                  if (total === 0) return '—'
                  return `${Math.round(((classification['INFORMED'] ?? 0) / total) * 100)}% informed`
                })()}
              </div>
            </div>
            <div className="w-full mt-3">
              <div className="h-2 bg-surface-3 rounded-full overflow-hidden flex">
                {(() => {
                  const total = (classification['INFORMED'] ?? 0) + (classification['NOISE'] ?? 0)
                  if (total === 0) return null
                  const informedPct = Math.round(((classification['INFORMED'] ?? 0) / total) * 100)
                  return (
                    <>
                      <div className="h-full bg-red-500 transition-all" style={{ width: `${informedPct}%` }} />
                      <div className="h-full bg-neutral-600 transition-all flex-1" />
                    </>
                  )
                })()}
              </div>
              <div className="flex justify-between text-[10px] text-neutral-600 mt-1">
                <span>INFORMED</span>
                <span>NOISE</span>
              </div>
            </div>
            {/* T2 Coverage */}
            {t2cov.total > 0 && (
              <div className="mt-3 pt-3 border-t border-border-subtle">
                <div className="text-[10px] uppercase tracking-wider text-neutral-600 mb-1">
                  T2 Coverage (Today)
                </div>
                <div className="flex items-center gap-3">
                  <div className="flex-1 h-1.5 bg-surface-3 rounded-full overflow-hidden flex">
                    <div
                      className="h-full bg-purple-500 transition-all"
                      style={{ width: `${t2CovPct ?? 0}%` }}
                    />
                  </div>
                  <span className="font-mono text-[11px] text-neutral-400 shrink-0">
                    {t2CovPct ?? 0}% real · {t2cov.fallback} fallback
                  </span>
                </div>
              </div>
            )}
          </div>

          {/* LLM Budget Gauge */}
          <div className="bg-surface-1 border border-border-subtle rounded-sm p-4">
            <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-3">
              LLM Budget (Today)
            </div>
            {/* T1 */}
            <div className="mb-2">
              <div className="flex items-center gap-3">
                <span className="text-[10px] text-neutral-600 w-4">T1</span>
                <div className="flex-1">
                  <div className="h-2.5 bg-surface-3 rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all ${
                        t1Pct > 80 ? 'bg-red-500' : t1Pct > 50 ? 'bg-amber-500' : 'bg-emerald-500'
                      }`}
                      style={{ width: `${t1Pct}%` }}
                    />
                  </div>
                </div>
                <span className="font-mono text-xs text-neutral-400 w-16 text-right">
                  {t1Used}/{t1Limit}
                </span>
              </div>
            </div>
            {/* T2 */}
            {(() => {
              const t2Used = budget?.tier2.calls_used ?? 0
              const t2Limit = budget?.tier2.calls_limit ?? 1
              const t2Pct = Math.min(100, Math.round((t2Used / t2Limit) * 100))
              return (
                <div>
                  <div className="flex items-center gap-3">
                    <span className="text-[10px] text-neutral-600 w-4">T2</span>
                    <div className="flex-1">
                      <div className="h-2.5 bg-surface-3 rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full transition-all ${
                            t2Pct > 80 ? 'bg-red-500' : t2Pct > 50 ? 'bg-amber-500' : 'bg-purple-500'
                          }`}
                          style={{ width: `${t2Pct}%` }}
                        />
                      </div>
                    </div>
                    <span className="font-mono text-xs text-neutral-400 w-16 text-right">
                      {t2Used}/{t2Limit}
                    </span>
                  </div>
                </div>
              )
            })()}
          </div>
        </div>
      </div>

      {/* ── Row 3: Hour-of-Day Pattern Chart ─────────────────────── */}
      {patterns.length > 0 && (
        <div className="bg-surface-1 border border-border-subtle rounded-sm p-4">
          <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-1">
            Hour-of-Day Trading Patterns (7d)
          </div>
          <div className="text-[10px] text-neutral-600 mb-3">
            Trades, signals, and INFORMED classifications by UTC hour
          </div>
          <div className="h-44">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={patterns.map((p) => ({
                  ...p,
                  label: `${String(p.hour).padStart(2, '0')}:00`,
                }))}
                margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
                barCategoryGap="20%"
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#1e1e1e" vertical={false} />
                <XAxis
                  dataKey="label"
                  tick={{ fill: '#525252', fontSize: 9 }}
                  axisLine={{ stroke: '#1e1e1e' }}
                  tickLine={false}
                  interval={3}
                />
                <YAxis
                  tick={{ fill: '#525252', fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                  allowDecimals={false}
                />
                <Tooltip
                  contentStyle={{
                    background: '#191919',
                    border: '1px solid #2a2a2a',
                    borderRadius: '3px',
                    fontSize: '11px',
                    color: '#d4d4d4',
                  }}
                  labelStyle={{ color: '#737373' }}
                />
                <Legend wrapperStyle={{ fontSize: '11px', color: '#737373' }} iconType="square" />
                <Bar dataKey="trades" fill="#404040" name="Trades" maxBarSize={16} />
                <Bar dataKey="signals" fill="#f59e0b" name="Signals" maxBarSize={16} />
                <Bar dataKey="informed" fill="#ef4444" name="INFORMED" maxBarSize={16} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* ── Row 4: Top Flagged Markets ───────────────────────────── */}
      <div className="bg-surface-1 border border-border-subtle rounded-sm">
        <div className="px-4 py-3 border-b border-border-subtle">
          <span className="text-[11px] uppercase tracking-wider text-neutral-500">
            Top Flagged Markets (24h)
          </span>
        </div>
        {topMarkets.length === 0 ? (
          <div className="py-12 text-center text-neutral-600 text-sm">
            No flagged markets in the last 24 hours.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border-subtle text-neutral-500 text-left">
                  <th className="py-2 px-4 font-medium">Market</th>
                  <th className="py-2 px-4 font-medium">Category</th>
                  <th className="py-2 px-4 font-medium text-right">Signals</th>
                  <th className="py-2 px-4 font-medium text-right">Max Score</th>
                  <th className="py-2 px-4 font-medium text-right">Avg Score</th>
                </tr>
              </thead>
              <tbody>
                {topMarkets.map((m) => (
                  <tr
                    key={m.market_id}
                    className="border-b border-border-subtle hover:bg-surface-2 transition-colors cursor-pointer"
                    onClick={() => navigate(`/market/${m.market_id}`)}
                  >
                    <td className="py-2 px-4 max-w-[400px] truncate text-neutral-300">
                      {m.question
                        ? m.question.length > 60
                          ? m.question.slice(0, 60) + '…'
                          : m.question
                        : m.market_id.slice(0, 16) + '…'}
                    </td>
                    <td className="py-2 px-4">
                      {m.category ? (
                        <span className="px-2 py-0.5 bg-surface-2 rounded-sm text-neutral-400">
                          {m.category}
                        </span>
                      ) : (
                        <span className="text-neutral-600">—</span>
                      )}
                    </td>
                    <td className="py-2 px-4 text-right font-mono text-amber-400">{m.signal_count}</td>
                    <td className="py-2 px-4 text-right font-mono text-neutral-300">{m.max_score ?? '—'}</td>
                    <td className="py-2 px-4 text-right font-mono text-neutral-400">{m.avg_score ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Row 5: Classification Accuracy (Resolved Markets) ────── */}
      {accuracy.length > 0 && (
        <div className="bg-surface-1 border border-border-subtle rounded-sm">
          <div className="px-4 py-3 border-b border-border-subtle flex items-center justify-between">
            <span className="text-[11px] uppercase tracking-wider text-neutral-500">
              Classification Accuracy — Resolved Markets
            </span>
            <span className="text-[10px] text-neutral-600">
              INFORMED prediction correctness vs. actual outcome
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border-subtle text-neutral-500 text-left">
                  <th className="py-2 px-4 font-medium">Market</th>
                  <th className="py-2 px-4 font-medium">Outcome</th>
                  <th className="py-2 px-4 font-medium text-right">Signals</th>
                  <th className="py-2 px-4 font-medium text-right">INFORMED</th>
                  <th className="py-2 px-4 font-medium text-right">Correct</th>
                  <th className="py-2 px-4 font-medium text-right">Accuracy</th>
                </tr>
              </thead>
              <tbody>
                {accuracy.map((m) => {
                  const resolved = m.resolved_price != null
                    ? m.resolved_price >= 0.95 ? 'YES' : m.resolved_price <= 0.05 ? 'NO' : `${Math.round(m.resolved_price * 100)}¢`
                    : '—'
                  const acc = m.accuracy_pct
                  return (
                    <tr
                      key={m.market_id}
                      className="border-b border-border-subtle hover:bg-surface-2 transition-colors cursor-pointer"
                      onClick={() => navigate(`/market/${m.market_id}`)}
                    >
                      <td className="py-2 px-4 max-w-[400px] truncate text-neutral-300">
                        {m.question
                          ? m.question.length > 60 ? m.question.slice(0, 60) + '…' : m.question
                          : m.market_id.slice(0, 16) + '…'}
                      </td>
                      <td className="py-2 px-4">
                        <span className={`font-mono font-medium ${
                          resolved === 'YES' ? 'text-emerald-400' : resolved === 'NO' ? 'text-red-400' : 'text-neutral-500'
                        }`}>
                          {resolved}
                        </span>
                      </td>
                      <td className="py-2 px-4 text-right font-mono text-neutral-400">{m.signal_count}</td>
                      <td className="py-2 px-4 text-right font-mono text-neutral-400">{m.informed_count}</td>
                      <td className="py-2 px-4 text-right font-mono text-neutral-400">{m.correct_informed}</td>
                      <td className="py-2 px-4 text-right font-mono">
                        {acc != null ? (
                          <span className={acc >= 70 ? 'text-emerald-400' : acc >= 40 ? 'text-amber-400' : 'text-red-400'}>
                            {acc}%
                          </span>
                        ) : (
                          <span className="text-neutral-600">—</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

/* ── Funnel Row Component ────────────────────────────────────────── */
function FunnelRow({
  label,
  value,
  total,
  accent,
}: {
  label: string
  value: number | undefined
  total: number | undefined
  accent?: boolean
}) {
  const v = value ?? 0
  const t = total && total > 0 ? total : 1
  const pct = Math.round((v / t) * 100)
  const barWidth = Math.max(2, pct)

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-xs text-neutral-400">{label}</span>
        <span className={`font-mono text-sm font-medium ${accent ? 'text-red-400' : 'text-neutral-100'}`}>
          {fmtNum(v)}
          <span className="text-neutral-600 text-[10px] ml-1">
            {total != null && total > 0 && label !== 'Trades' ? `${pct}%` : ''}
          </span>
        </span>
      </div>
      <div className="h-1.5 bg-surface-3 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${accent ? 'bg-red-500' : 'bg-amber-500'}`}
          style={{ width: `${barWidth}%` }}
        />
      </div>
    </div>
  )
}

/* ── Ingestion Card Component ────────────────────────────────────── */
function IngestionCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string
  value: number | undefined
  sub?: string
  accent?: boolean
}) {
  return (
    <div className="bg-surface-1 border border-border-subtle rounded-sm px-4 py-3">
      <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-1">
        {label}
      </div>
      <div
        className={`font-mono text-lg font-medium ${
          accent ? 'text-blue-400' : 'text-neutral-100'
        }`}
      >
        {fmtNum(value)}
      </div>
      {sub && (
        <div className="text-[11px] text-neutral-600 mt-0.5">{sub}</div>
      )}
    </div>
  )
}
