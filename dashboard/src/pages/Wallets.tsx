import { useNavigate } from 'react-router-dom'
import { useWallets } from '../hooks/queries'
import { fmtNum, fmtPct, truncAddr } from '../lib/format'

export default function Wallets() {
  const { data: wallets = [], isLoading } = useWallets(50)
  const navigate = useNavigate()

  return (
    <div className="px-5 py-4 space-y-4 max-w-[1600px] mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-base font-semibold text-neutral-100">Smart Money Leaderboard</h1>
          <p className="text-xs text-neutral-500 mt-0.5">
            Wallets ranked by win rate × resolved trades on Polymarket. Requires ≥ 5 resolved positions.
          </p>
        </div>
        <span className="text-xs text-neutral-700">· Refresh 1m</span>
      </div>

      {/* Leaderboard Table */}
      <div className="bg-surface-1 border border-border-subtle rounded-sm">
        {isLoading ? (
          <div className="py-16 text-center text-neutral-600 text-sm">Loading…</div>
        ) : wallets.length === 0 ? (
          <div className="py-16 text-center text-neutral-600 text-sm">
            No wallets with ≥ 5 resolved trades yet.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border-subtle text-neutral-500 text-left">
                  <th className="py-2 px-4 font-medium w-10">#</th>
                  <th className="py-2 px-4 font-medium">Wallet</th>
                  <th className="py-2 px-4 font-medium text-right">Win Rate</th>
                  <th className="py-2 px-4 font-medium text-right">Wins</th>
                  <th className="py-2 px-4 font-medium text-right">Resolved</th>
                  <th className="py-2 px-4 font-medium text-right">Total Trades</th>
                  <th className="py-2 px-4 font-medium text-right">Signals</th>
                  <th className="py-2 px-4 font-medium text-right">Hit Rate</th>
                  <th className="py-2 px-4 font-medium text-right">Score</th>
                </tr>
              </thead>
              <tbody>
                {wallets.map((w, i) => {
                  const smartScore = Math.round(w.win_rate * w.resolved_trades * 10) / 10
                  const hitPct = w.signal_hit_rate != null
                    ? Math.round(w.signal_hit_rate * 1000) / 10
                    : null
                  return (
                    <tr
                      key={w.wallet}
                      className="border-b border-border-subtle hover:bg-surface-2 transition-colors cursor-pointer"
                      onClick={() => navigate(`/wallet/${w.wallet}`)}
                    >
                      {/* Rank */}
                      <td className="py-2 px-4 font-mono text-neutral-600">{i + 1}</td>

                      {/* Wallet */}
                      <td className="py-2 px-4">
                        <div className="font-mono text-neutral-300">{truncAddr(w.wallet, 8)}</div>
                        <div className="text-[10px] text-neutral-600">{w.wallet.slice(0, 10)}…</div>
                      </td>

                      {/* Win Rate — bar */}
                      <td className="py-2 px-4 text-right">
                        <div className="flex items-center justify-end gap-2">
                          <div className="w-16 h-1.5 bg-surface-3 rounded-full overflow-hidden">
                            <div
                              className={`h-full rounded-full ${
                                w.win_rate >= 0.7 ? 'bg-emerald-500' : w.win_rate >= 0.55 ? 'bg-amber-500' : 'bg-neutral-500'
                              }`}
                              style={{ width: `${Math.round(w.win_rate * 100)}%` }}
                            />
                          </div>
                          <span className={`font-mono font-medium w-10 ${
                            w.win_rate >= 0.7 ? 'text-emerald-400' : w.win_rate >= 0.55 ? 'text-amber-400' : 'text-neutral-400'
                          }`}>
                            {fmtPct(w.win_rate)}
                          </span>
                        </div>
                      </td>

                      {/* Wins */}
                      <td className="py-2 px-4 text-right font-mono text-neutral-400">{fmtNum(w.wins)}</td>

                      {/* Resolved */}
                      <td className="py-2 px-4 text-right font-mono text-neutral-400">{fmtNum(w.resolved_trades)}</td>

                      {/* Total Trades */}
                      <td className="py-2 px-4 text-right font-mono text-neutral-500">{fmtNum(w.total_trades)}</td>

                      {/* Signal Count */}
                      <td className="py-2 px-4 text-right font-mono text-amber-400">{fmtNum(w.signal_count)}</td>

                      {/* Signal Hit Rate */}
                      <td className="py-2 px-4 text-right font-mono text-neutral-500">
                        {hitPct != null ? `${hitPct}%` : '—'}
                      </td>

                      {/* Smart Score = win_rate × resolved_trades */}
                      <td className="py-2 px-4 text-right font-mono text-neutral-300 font-medium">
                        {smartScore.toFixed(1)}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Legend */}
      <div className="text-[10px] text-neutral-700 space-y-0.5">
        <div>
          <span className="text-neutral-600">Score</span> = win_rate × resolved_trades — higher means more resolved positions with higher accuracy.
        </div>
        <div>
          <span className="text-neutral-600">Hit Rate</span> = signals / total trades — what fraction of this wallet's trades were flagged by the pipeline.
        </div>
      </div>
    </div>
  )
}
