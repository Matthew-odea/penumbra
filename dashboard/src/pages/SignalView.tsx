import { useParams, Link } from 'react-router-dom'
import { useSignalDetail } from '../hooks/queries'
import { fmtUsd, fmtPrice, truncAddr, timeAgo, scoreBg, sideColor } from '../lib/format'

/* ── Feature cell color thresholds ─────────────────────────────────── */

function featureColor(level: 'green' | 'amber' | 'red' | 'muted'): string {
  switch (level) {
    case 'green':
      return 'text-emerald-400'
    case 'amber':
      return 'text-amber-400'
    case 'red':
      return 'text-red-400'
    case 'muted':
      return 'text-neutral-500'
  }
}

function featureDot(level: 'green' | 'amber' | 'red' | 'muted'): string {
  switch (level) {
    case 'green':
      return 'bg-emerald-400'
    case 'amber':
      return 'bg-amber-400'
    case 'red':
      return 'bg-red-400'
    case 'muted':
      return 'bg-neutral-600'
  }
}

interface FeatureRowProps {
  label: string
  value: string
  level: 'green' | 'amber' | 'red' | 'muted'
}

function FeatureRow({ label, value, level }: FeatureRowProps) {
  return (
    <div className="flex items-center justify-between py-1.5 border-b border-border-subtle last:border-b-0">
      <span className="text-[11px] text-neutral-500">{label}</span>
      <div className="flex items-center gap-2">
        <span className={`font-mono text-[11px] ${featureColor(level)}`}>{value}</span>
        <span className={`inline-block w-1.5 h-1.5 rounded-full ${featureDot(level)}`} />
      </div>
    </div>
  )
}

export default function SignalView() {
  const { signalId } = useParams<{ signalId: string }>()
  const { data: signal, isLoading, isError } = useSignalDetail(signalId!)

  if (isLoading) {
    return (
      <div className="px-5 py-4">
        <div className="text-neutral-600 text-sm">Loading signal...</div>
      </div>
    )
  }

  if (isError || !signal) {
    return (
      <div className="px-5 py-4">
        <div className="text-red-400 text-sm">Signal not found.</div>
      </div>
    )
  }

  const s = signal
  const score = s.statistical_score

  /* ── Build feature rows ──────────────────────────────────────────── */

  function zScoreLevel(z: number | null): 'green' | 'amber' | 'red' | 'muted' {
    if (z == null) return 'muted'
    if (z >= 5) return 'red'
    if (z >= 3) return 'amber'
    return 'green'
  }

  function ofiLevel(ofi: number | null): 'green' | 'amber' | 'red' | 'muted' {
    if (ofi == null) return 'muted'
    const abs = Math.abs(ofi)
    if (abs >= 0.7) return 'red'
    if (abs >= 0.4) return 'amber'
    return 'green'
  }

  function priceImpactLevel(pi: number | null): 'green' | 'amber' | 'red' | 'muted' {
    if (pi == null) return 'muted'
    if (pi >= 0.05) return 'red'
    if (pi >= 0.01) return 'amber'
    return 'green'
  }

  function winRateLevel(wr: number | null): 'green' | 'amber' | 'red' | 'muted' {
    if (wr == null) return 'muted'
    if (wr >= 0.75) return 'red'
    if (wr >= 0.6) return 'amber'
    return 'green'
  }

  function fundingLevel(anomaly: boolean, age: number | null): 'green' | 'amber' | 'red' | 'muted' {
    if (!anomaly) return 'green'
    if (age != null && age < 15) return 'red'
    if (age != null && age < 60) return 'amber'
    return 'amber'
  }

  function concentrationLevel(c: number | null): 'green' | 'amber' | 'red' | 'muted' {
    if (c == null) return 'muted'
    if (c >= 0.8) return 'red'
    if (c >= 0.5) return 'amber'
    return 'green'
  }

  function coordinationLevel(count: number | null): 'green' | 'amber' | 'red' | 'muted' {
    if (count == null || count < 3) return 'muted'
    if (count >= 5) return 'red'
    return 'amber'
  }

  function hoursLevel(h: number | null): 'green' | 'amber' | 'red' | 'muted' {
    if (h == null) return 'muted'
    if (h < 24) return 'red'
    if (h < 72) return 'amber'
    return 'green'
  }

  // Extract extended fields from the signal detail response
  const vpinPercentile = (s as Record<string, unknown>).vpin_percentile as number | null | undefined
  const lambdaValue = (s as Record<string, unknown>).lambda_value as number | null | undefined
  const scoringVersion = (s as Record<string, unknown>).scoring_version as string | null | undefined

  const leftFeatures: FeatureRowProps[] = [
    {
      label: 'Volume Z-score',
      value: s.modified_z_score?.toFixed(2) ?? '--',
      level: zScoreLevel(s.modified_z_score),
    },
    {
      label: 'OFI Score',
      value: s.ofi_score != null ? `${s.ofi_score > 0 ? '+' : ''}${(s.ofi_score * 100).toFixed(0)}%` : '--',
      level: ofiLevel(s.ofi_score),
    },
    {
      label: 'Price Impact',
      value: s.price_impact != null ? `${(s.price_impact * 100).toFixed(2)}%` : '--',
      level: priceImpactLevel(s.price_impact),
    },
    {
      label: 'Wallet Win Rate',
      value: s.wallet_win_rate != null ? `${(s.wallet_win_rate * 100).toFixed(0)}%` : '--',
      level: winRateLevel(s.wallet_win_rate),
    },
    {
      label: 'Wallet Total Trades',
      value: s.wallet_total_trades?.toLocaleString() ?? '--',
      level: s.wallet_total_trades != null && s.wallet_total_trades > 100 ? 'amber' : 'muted',
    },
    {
      label: 'Whitelisted',
      value: s.is_whitelisted ? 'Yes' : 'No',
      level: s.is_whitelisted ? 'green' : 'muted',
    },
    {
      label: 'Funding Anomaly',
      value: s.funding_anomaly ? `Yes (${s.funding_age_minutes ?? '--'}m)` : 'No',
      level: fundingLevel(s.funding_anomaly, s.funding_age_minutes),
    },
    {
      label: 'Funding Age',
      value: s.funding_age_minutes != null ? `${s.funding_age_minutes}m` : '--',
      level: fundingLevel(s.funding_anomaly, s.funding_age_minutes),
    },
  ]

  const rightFeatures: FeatureRowProps[] = [
    {
      label: 'Market Concentration',
      value: s.market_concentration != null ? `${Math.round(s.market_concentration * 100)}%` : '--',
      level: concentrationLevel(s.market_concentration),
    },
    {
      label: 'Coordination Count',
      value: s.coordination_wallet_count?.toString() ?? '--',
      level: coordinationLevel(s.coordination_wallet_count),
    },
    {
      label: 'Liquidity Cliff',
      value: s.liquidity_cliff ? 'Detected' : 'None',
      level: s.liquidity_cliff ? 'red' : 'green',
    },
    {
      label: 'Position Trade Count',
      value: s.position_trade_count?.toString() ?? '--',
      level: s.position_trade_count != null && s.position_trade_count >= 5 ? 'amber' : 'muted',
    },
    {
      label: 'VPIN Percentile',
      value: vpinPercentile != null ? `${(vpinPercentile * 100).toFixed(0)}%` : '--',
      level: vpinPercentile != null && vpinPercentile >= 0.8 ? 'red' : vpinPercentile != null && vpinPercentile >= 0.5 ? 'amber' : 'muted',
    },
    {
      label: 'Lambda Value',
      value: lambdaValue != null ? lambdaValue.toFixed(4) : '--',
      level: lambdaValue != null && lambdaValue >= 0.01 ? 'red' : lambdaValue != null && lambdaValue >= 0.001 ? 'amber' : 'muted',
    },
    {
      label: 'Hours to Resolution',
      value: s.hours_to_resolution != null
        ? (s.hours_to_resolution < 24 ? `${s.hours_to_resolution}h` : `${Math.round(s.hours_to_resolution / 24)}d`)
        : '--',
      level: hoursLevel(s.hours_to_resolution),
    },
    {
      label: 'Scoring Version',
      value: scoringVersion ?? '--',
      level: 'muted',
    },
  ]

  return (
    <div className="px-5 py-4 space-y-4 max-w-[1600px] mx-auto">
      {/* Breadcrumb */}
      <div className="flex items-center gap-2 text-xs text-neutral-500">
        <Link to="/" className="hover:text-neutral-300 transition-colors">Feed</Link>
        <span className="text-neutral-700">&rsaquo;</span>
        <span className="text-neutral-400">Signal</span>
      </div>

      {/* Header */}
      <div className="space-y-2">
        <div className="flex items-start gap-3">
          <span className={`inline-block px-2.5 py-1 rounded-sm font-mono font-semibold text-sm ${scoreBg(score)}`}>
            {score}
          </span>
          <div className="flex-1 min-w-0">
            <Link
              to={`/market/${s.market_id}`}
              className="text-base font-semibold text-neutral-100 leading-tight hover:text-accent transition-colors block truncate"
              title={s.market_question ?? s.market_id}
            >
              {s.market_question ?? s.market_id}
            </Link>
          </div>
        </div>
        <div className="flex items-center gap-4 text-xs text-neutral-500">
          <span className={`font-mono font-medium ${sideColor(s.side)}`}>{s.side}</span>
          <span>Price: <span className="text-neutral-300 font-mono">{fmtPrice(s.price)}</span></span>
          <span>Size: <span className="text-neutral-300 font-mono">{fmtUsd(s.size_usd)}</span></span>
          <span>
            Wallet:{' '}
            {s.wallet ? (
              <Link
                to={`/wallet/${s.wallet}`}
                className="font-mono text-neutral-400 hover:text-accent transition-colors"
                title={s.wallet}
              >
                {truncAddr(s.wallet)}
              </Link>
            ) : (
              <span className="text-neutral-600">unknown (WS trade)</span>
            )}
          </span>
          <span className="text-neutral-600">{timeAgo(s.created_at)}</span>
        </div>
      </div>

      {/* Explanation */}
      {s.explanation && (
        <div className="bg-surface-1 border border-border-subtle rounded-sm px-4 py-3">
          <div className="text-[10px] uppercase tracking-wider text-neutral-600 mb-1">Why Suspicious</div>
          <p className="text-sm text-neutral-200 leading-relaxed">{s.explanation}</p>
        </div>
      )}

      {/* Feature Breakdown Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-surface-1 border border-border-subtle rounded-sm px-4 py-3">
          <div className="text-[10px] uppercase tracking-wider text-neutral-600 mb-2">Signal Dimensions (1/2)</div>
          {leftFeatures.map((f) => (
            <FeatureRow key={f.label} {...f} />
          ))}
        </div>
        <div className="bg-surface-1 border border-border-subtle rounded-sm px-4 py-3">
          <div className="text-[10px] uppercase tracking-wider text-neutral-600 mb-2">Signal Dimensions (2/2)</div>
          {rightFeatures.map((f) => (
            <FeatureRow key={f.label} {...f} />
          ))}
        </div>
      </div>

      {/* Links */}
      <div className="flex items-center gap-4 text-xs">
        <Link
          to={`/market/${s.market_id}`}
          className="px-3 py-1.5 bg-surface-2 border border-border-subtle rounded-sm text-neutral-400 hover:text-neutral-200 transition-colors"
        >
          View Market Detail
        </Link>
        {s.wallet && (
          <Link
            to={`/wallet/${s.wallet}`}
            className="px-3 py-1.5 bg-surface-2 border border-border-subtle rounded-sm text-neutral-400 hover:text-neutral-200 transition-colors"
          >
            View Wallet Profile
          </Link>
        )}
        {String((s as Record<string, unknown>).tx_hash ?? '') !== '' && (
          <a
            href={`https://polygonscan.com/tx/${String((s as Record<string, unknown>).tx_hash)}`}
            target="_blank"
            rel="noopener noreferrer"
            className="px-3 py-1.5 bg-surface-2 border border-border-subtle rounded-sm text-neutral-400 hover:text-neutral-200 transition-colors"
          >
            View on Polygonscan
          </a>
        )}
      </div>
    </div>
  )
}
