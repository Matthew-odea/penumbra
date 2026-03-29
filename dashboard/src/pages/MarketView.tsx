import { useParams, Link } from 'react-router-dom'
import { useMarketDetail, useMarketVolume, useMarketSignals, useMarketAnomalies, useMarketVPIN, useMarketLambda } from '../hooks/queries'
import { fmtUsd } from '../lib/format'
import VolumeChart from '../components/VolumeChart'
import SignalTable from '../components/SignalTable'
import {
  AreaChart,
  Area,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from 'recharts'
import type { VPINPoint, LambdaPoint } from '../api/types'

function TierBadge({ tier }: { tier: 'hot' | 'scored' | 'unscored' | undefined }) {
  if (tier === 'hot') return (
    <span className="inline-block px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide rounded-sm bg-red-500/15 text-red-400 border border-red-500/20">
      HOT TIER
    </span>
  )
  if (tier === 'scored') return (
    <span className="inline-block px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide rounded-sm bg-surface-3 text-neutral-500">
      scored
    </span>
  )
  if (tier === 'unscored') return (
    <span className="inline-block px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide rounded-sm bg-surface-2 text-neutral-600 border border-border-subtle">
      scoring…
    </span>
  )
  return null
}

/* ── VPIN Chart ────────────────────────────────────────────────────── */

function VPINChart({ data }: { data: VPINPoint[] }) {
  if (data.length === 0) {
    return (
      <div className="h-48 flex items-center justify-center text-neutral-600 text-xs">
        No VPIN data yet
      </div>
    )
  }

  const formatted = data.map((d) => ({
    ...d,
    label: new Date(d.timestamp).toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    }),
  }))

  return (
    <div className="h-48">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={formatted} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
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
            domain={[0, 1]}
            tickFormatter={(v: number) => v.toFixed(1)}
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
            formatter={(value: number) => [value?.toFixed(3), 'Imbalance']}
          />
          <defs>
            <linearGradient id="vpinGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#f59e0b" stopOpacity={0.4} />
              <stop offset="100%" stopColor="#f59e0b" stopOpacity={0.05} />
            </linearGradient>
          </defs>
          <Area
            type="monotone"
            dataKey="imbalance"
            stroke="#f59e0b"
            strokeWidth={1.5}
            fill="url(#vpinGrad)"
            connectNulls
            dot={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

/* ── Lambda Chart ──────────────────────────────────────────────────── */

function LambdaChart({ data }: { data: LambdaPoint[] }) {
  if (data.length === 0) {
    return (
      <div className="h-48 flex items-center justify-center text-neutral-600 text-xs">
        No Lambda data yet
      </div>
    )
  }

  const formatted = data.map((d) => ({
    ...d,
    label: new Date(d.timestamp).toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    }),
  }))

  return (
    <div className="h-48">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={formatted} margin={{ top: 4, right: 36, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e1e1e" vertical={false} />
          <XAxis
            dataKey="label"
            tick={{ fill: '#525252', fontSize: 10 }}
            axisLine={{ stroke: '#1e1e1e' }}
            tickLine={false}
            interval="preserveStartEnd"
          />
          <YAxis
            yAxisId="lambda"
            tick={{ fill: '#525252', fontSize: 10 }}
            axisLine={false}
            tickLine={false}
            tickFormatter={(v: number) => v.toFixed(4)}
          />
          <YAxis
            yAxisId="r2"
            orientation="right"
            tick={{ fill: '#525252', fontSize: 10 }}
            axisLine={false}
            tickLine={false}
            domain={[0, 1]}
            tickFormatter={(v: number) => v.toFixed(1)}
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
            formatter={(value: number, name: string) => {
              if (name === 'R-squared') return [value?.toFixed(3), 'R-squared']
              return [value?.toFixed(6), 'Lambda']
            }}
          />
          <Line
            yAxisId="lambda"
            type="monotone"
            dataKey="lambda_value"
            stroke="#818cf8"
            strokeWidth={1.5}
            dot={false}
            name="Lambda"
            connectNulls
          />
          <Line
            yAxisId="r2"
            type="monotone"
            dataKey="r_squared"
            stroke="#525252"
            strokeWidth={1}
            strokeDasharray="4 3"
            dot={false}
            name="R-squared"
            connectNulls
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

export default function MarketView() {
  const { marketId } = useParams<{ marketId: string }>()
  const { data: market, isLoading: loadingMarket } = useMarketDetail(marketId!)
  const { data: volume = [] } = useMarketVolume(marketId!)
  const { data: anomalies = [] } = useMarketAnomalies(marketId!)
  const { data: signals = [] } = useMarketSignals(marketId!)
  const { data: vpin = [] } = useMarketVPIN(marketId!)
  const { data: lambda = [] } = useMarketLambda(marketId!)

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
        <div className="flex items-start gap-3">
          <h1 className="text-base font-semibold text-neutral-100 leading-tight flex-1">
            {market.question}
          </h1>
          <TierBadge tier={market.tier} />
        </div>
        <div className="flex items-center gap-4 text-xs text-neutral-500">
          {market.category && (
            <span className="px-2 py-0.5 bg-surface-2 rounded-sm text-neutral-400">
              {market.category}
            </span>
          )}
          <span>Volume: <span className="text-neutral-300 font-mono">{fmtUsd(market.volume_usd)}</span></span>
          <span>Liquidity: <span className="text-neutral-300 font-mono">{fmtUsd(market.liquidity_usd)}</span></span>
          {market.last_price != null && (
            <span>YES price: <span className="text-neutral-300 font-mono">{Math.round(market.last_price * 100)}%</span></span>
          )}
          {market.hours_to_resolution != null && (
            <span className={market.hours_to_resolution < 24 ? 'text-red-400' : market.hours_to_resolution < 72 ? 'text-amber-400' : ''}>
              {market.hours_to_resolution < 24
                ? `${market.hours_to_resolution}h to resolution`
                : `${Math.round(market.hours_to_resolution / 24)}d to resolution`}
            </span>
          )}
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

      {/* Attractiveness band */}
      {market.attractiveness_score != null && (
        <div className="bg-surface-1 border border-border-subtle rounded-sm px-4 py-3 flex items-start gap-4">
          <div className="shrink-0">
            <div className="text-[10px] uppercase tracking-wider text-neutral-600 mb-1">Attractiveness</div>
            <span className={`inline-block px-2.5 py-1 rounded-sm font-mono font-semibold text-sm ${
              market.attractiveness_score >= 80 ? 'bg-red-500/20 text-red-300' :
              market.attractiveness_score >= 60 ? 'bg-amber-500/20 text-amber-300' :
              'bg-surface-3 text-neutral-400'
            }`}>
              {market.attractiveness_score} / 100
            </span>
          </div>
          {market.attractiveness_reason && (
            <div className="flex-1">
              <div className="text-[10px] uppercase tracking-wider text-neutral-600 mb-1">Why we're watching</div>
              <p className="text-xs text-neutral-300">{market.attractiveness_reason}</p>
            </div>
          )}
        </div>
      )}

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

      {/* VPIN Chart */}
      <div className="bg-surface-1 border border-border-subtle rounded-sm p-4">
        <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-3">
          VPIN — Flow Toxicity
        </div>
        <VPINChart data={vpin} />
        {vpin.length > 0 && (
          <div className="text-[10px] text-neutral-600 mt-1">
            Imbalance approaching 1.0 indicates one-sided (toxic) flow. Values below 0.4 are typical.
          </div>
        )}
      </div>

      {/* Lambda Chart */}
      <div className="bg-surface-1 border border-border-subtle rounded-sm p-4">
        <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-3">
          Kyle's Lambda — Price Impact Coefficient
        </div>
        <LambdaChart data={lambda} />
        {lambda.length > 0 && (
          <div className="text-[10px] text-neutral-600 mt-1 flex items-center gap-3">
            <span className="flex items-center gap-1">
              <span className="inline-block w-3 h-px bg-indigo-400" />
              Lambda (left axis)
            </span>
            <span className="flex items-center gap-1">
              <span className="inline-block w-3 h-px bg-neutral-500 border-dashed" />
              R-squared (right axis)
            </span>
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
