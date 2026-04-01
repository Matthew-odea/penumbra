import { Fragment, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { Signal } from '../api/types'
import { fmtUsd, fmtPrice, truncAddr, timeAgo, scoreBg, sideColor } from '../lib/format'

interface Props {
  signals: Signal[]
}

export default function SignalTable({ signals }: Props) {
  const [expanded, setExpanded] = useState<string | null>(null)
  const navigate = useNavigate()

  if (signals.length === 0) {
    return (
      <div className="text-center py-16 text-neutral-600 text-sm">
        No signals found matching filters.
      </div>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-border-subtle text-neutral-500 text-left">
            <th className="py-2 px-3 font-medium w-5"></th>
            <th className="py-2 px-3 font-medium">Score</th>
            <th className="py-2 px-3 font-medium">Market</th>
            <th className="py-2 px-3 font-medium">Wallet</th>
            <th className="py-2 px-3 font-medium">Side</th>
            <th className="py-2 px-3 font-medium text-right">Price</th>
            <th className="py-2 px-3 font-medium text-right">Size</th>
            <th className="py-2 px-3 font-medium text-right">Z-Score</th>
            <th className="py-2 px-3 font-medium text-right">Win Rate</th>
            <th className="py-2 px-3 font-medium text-right">Time</th>
          </tr>
        </thead>
        <tbody>
          {signals.map((s) => {
            const score = s.statistical_score
            const isExpanded = expanded === s.signal_id
            const hasDetail = true

            return (
              <Fragment key={s.signal_id}>
                <tr
                  className={`border-b border-border-subtle hover:bg-surface-2 transition-colors ${
                    hasDetail ? 'cursor-pointer' : ''
                  } ${isExpanded ? 'bg-surface-2' : ''}`}
                  onClick={() => hasDetail && setExpanded(isExpanded ? null : s.signal_id)}
                >
                  {/* Expand icon */}
                  <td className="py-2 px-3 text-neutral-600">
                    {hasDetail && (
                      <span className={`inline-block transition-transform duration-150 ${isExpanded ? 'rotate-90' : ''}`}>
                        ›
                      </span>
                    )}
                  </td>

                  {/* Score badge (links to signal detail) */}
                  <td className="py-2 px-3">
                    <button
                      onClick={(e) => { e.stopPropagation(); navigate(`/signal/${s.signal_id}`) }}
                      className={`inline-block px-2 py-0.5 rounded-sm font-mono font-medium text-[11px] hover:ring-1 hover:ring-neutral-500 transition-all ${scoreBg(score)}`}
                      title="View signal detail"
                    >
                      {score ?? '\u2014'}
                    </button>
                  </td>

                  {/* Market */}
                  <td className="py-2 px-3 max-w-[280px]">
                    <div className="flex items-center gap-1.5">
                      <button
                        onClick={(e) => { e.stopPropagation(); navigate(`/market/${s.market_id}`) }}
                        className="text-left hover:text-accent transition-colors truncate"
                        title={s.market_question ?? s.market_id}
                      >
                        {s.market_question
                          ? s.market_question.length > 48
                            ? s.market_question.slice(0, 48) + '…'
                            : s.market_question
                          : s.market_id.slice(0, 12) + '…'}
                      </button>
                      {s.attractiveness_score !== null && s.attractiveness_score !== undefined && (
                        <span
                          className={`shrink-0 inline-block px-1 py-0 rounded-sm font-mono text-[9px] font-medium ${
                            s.attractiveness_score >= 80 ? 'bg-red-500/20 text-red-400' :
                            s.attractiveness_score >= 60 ? 'bg-amber-500/20 text-amber-400' :
                            'bg-surface-3 text-neutral-500'
                          }`}
                          title={s.attractiveness_reason ?? undefined}
                        >
                          {s.attractiveness_score}
                        </span>
                      )}
                    </div>
                    {s.category && (
                      <span className="text-[10px] text-neutral-600">{s.category}</span>
                    )}
                  </td>

                  {/* Wallet */}
                  <td className="py-2 px-3">
                    {s.wallet ? (
                      <button
                        onClick={(e) => { e.stopPropagation(); navigate(`/wallet/${s.wallet}`) }}
                        className="font-mono text-neutral-400 hover:text-accent transition-colors"
                        title={s.wallet}
                      >
                        {truncAddr(s.wallet)}
                      </button>
                    ) : (
                      <span className="text-neutral-600 text-[10px]">WS</span>
                    )}
                  </td>

                  {/* Side */}
                  <td className={`py-2 px-3 font-mono font-medium ${sideColor(s.side)}`}>
                    {s.side}
                  </td>

                  {/* Price */}
                  <td className="py-2 px-3 text-right font-mono text-neutral-300">
                    {fmtPrice(s.price)}
                  </td>

                  {/* Size */}
                  <td className="py-2 px-3 text-right font-mono text-neutral-300">
                    {fmtUsd(s.size_usd)}
                  </td>

                  {/* Z-Score */}
                  <td className="py-2 px-3 text-right font-mono">
                    <span className={s.modified_z_score && s.modified_z_score > 3.5 ? 'text-amber-400' : 'text-neutral-500'}>
                      {s.modified_z_score?.toFixed(1) ?? '—'}
                    </span>
                  </td>

                  {/* Win Rate */}
                  <td className="py-2 px-3 text-right font-mono">
                    <span className={s.wallet_win_rate && s.wallet_win_rate > 0.65 ? 'text-emerald-400' : 'text-neutral-500'}>
                      {s.wallet_win_rate != null ? `${(s.wallet_win_rate * 100).toFixed(0)}%` : '—'}
                    </span>
                  </td>

                  {/* Time */}
                  <td className="py-2 px-3 text-right text-neutral-500">
                    {timeAgo(s.created_at)}
                  </td>
                </tr>

                {/* Expanded detail row */}
                {isExpanded && hasDetail && (
                  <tr className="bg-surface-2 border-b border-border-subtle">
                    <td colSpan={10} className="px-6 py-3">
                      <div className="grid grid-cols-2 gap-6 text-xs">
                        {/* Left: Explanation */}
                        <div className="space-y-3">
                          {s.explanation && (
                            <div>
                              <div className="text-[10px] uppercase tracking-wider text-neutral-500 mb-1">
                                Why Suspicious
                              </div>
                              <p className="text-neutral-300 leading-relaxed">{s.explanation}</p>
                            </div>
                          )}
                        </div>

                        {/* Right: Signal Metrics */}
                        <div className="space-y-3">
                          {s.attractiveness_reason && (
                            <div>
                              <div className="text-[10px] uppercase tracking-wider text-neutral-500 mb-1">
                                Why We Watched
                              </div>
                              <p className="text-neutral-400">{s.attractiveness_reason}</p>
                            </div>
                          )}
                          <div className="flex flex-wrap gap-6">
                            {s.ofi_score != null && (
                              <div>
                                <div className="text-[10px] uppercase tracking-wider text-neutral-500 mb-0.5">
                                  Flow Imbalance
                                </div>
                                <span className={`font-mono ${
                                  s.ofi_score > 0.4 ? 'text-emerald-400'
                                  : s.ofi_score < -0.4 ? 'text-red-400'
                                  : 'text-neutral-500'
                                }`}>
                                  {s.ofi_score > 0 ? '+' : ''}{(s.ofi_score * 100).toFixed(0)}%
                                  {Math.abs(s.ofi_score) >= 0.4 ? (s.ofi_score > 0 ? ' BUY' : ' SELL') : ' neutral'}
                                </span>
                              </div>
                            )}
                            {s.hours_to_resolution != null && (
                              <div>
                                <div className="text-[10px] uppercase tracking-wider text-neutral-500 mb-0.5">
                                  Time to Resolve
                                </div>
                                <span className={`font-mono ${s.hours_to_resolution < 24 ? 'text-red-400' : s.hours_to_resolution < 72 ? 'text-amber-400' : 'text-neutral-500'}`}>
                                  {s.hours_to_resolution < 24
                                    ? `${s.hours_to_resolution}h`
                                    : `${Math.round(s.hours_to_resolution / 24)}d`}
                                </span>
                              </div>
                            )}
                            {s.market_concentration != null && s.market_concentration > 0.3 && (
                              <div>
                                <div className="text-[10px] uppercase tracking-wider text-neutral-500 mb-0.5">
                                  Concentration
                                </div>
                                <span className={`font-mono ${s.market_concentration >= 0.8 ? 'text-red-400' : s.market_concentration >= 0.5 ? 'text-amber-400' : 'text-neutral-500'}`}>
                                  {Math.round(s.market_concentration * 100)}% this market
                                </span>
                              </div>
                            )}
                            {s.funding_anomaly && (
                              <div>
                                <div className="text-[10px] uppercase tracking-wider text-neutral-500 mb-0.5">
                                  Funding
                                </div>
                                <span className="font-mono text-amber-400">
                                  Anomaly ({s.funding_age_minutes}m)
                                </span>
                              </div>
                            )}
                            {s.price_impact != null && (
                              <div>
                                <div className="text-[10px] uppercase tracking-wider text-neutral-500 mb-0.5">
                                  Price Impact
                                </div>
                                <span className="font-mono text-neutral-300">
                                  {(s.price_impact * 100).toFixed(2)}%
                                </span>
                              </div>
                            )}
                          </div>
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
