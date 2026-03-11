import { useParams, Link, useNavigate } from 'react-router-dom'
import { useWalletProfile, useWalletTrades, useWalletSignals } from '../hooks/queries'
import { fmtUsd, fmtPct, fmtPrice, truncAddr, timeAgo, sideColor } from '../lib/format'
import SignalTable from '../components/SignalTable'

export default function WalletView() {
  const { address } = useParams<{ address: string }>()
  const navigate = useNavigate()
  const { data: profile, isLoading } = useWalletProfile(address!)
  const { data: trades = [] } = useWalletTrades(address!)
  const { data: signals = [] } = useWalletSignals(address!)

  if (isLoading) {
    return (
      <div className="px-5 py-4">
        <div className="text-neutral-600 text-sm">Loading wallet…</div>
      </div>
    )
  }

  if (!profile) {
    return (
      <div className="px-5 py-4">
        <div className="text-red-400 text-sm">Failed to load wallet profile.</div>
      </div>
    )
  }

  return (
    <div className="px-5 py-4 space-y-4 max-w-[1600px] mx-auto">
      {/* Breadcrumb */}
      <div className="flex items-center gap-2 text-xs text-neutral-500">
        <Link to="/" className="hover:text-neutral-300 transition-colors">Feed</Link>
        <span className="text-neutral-700">›</span>
        <span className="text-neutral-400">Wallet</span>
      </div>

      {/* Header */}
      <div className="space-y-1">
        <h1 className="text-base font-mono font-medium text-neutral-100">
          {address}
        </h1>
        <div className="text-xs text-neutral-500">
          {truncAddr(address!, 6)} · {profile.total_trades} trades · {profile.signal_count} signal{profile.signal_count !== 1 ? 's' : ''}
        </div>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-4 gap-3">
        <StatCard label="Total Trades" value={String(profile.total_trades)} />
        <StatCard label="Resolved Trades" value={String(profile.resolved_trades)} />
        <StatCard
          label="Win Rate"
          value={fmtPct(profile.win_rate)}
          accent={profile.win_rate != null && profile.win_rate > 0.65}
        />
        <StatCard label="Wins" value={String(profile.wins)} />
      </div>

      {/* Category Breakdown */}
      {profile.categories.length > 0 && (
        <div className="bg-surface-1 border border-border-subtle rounded-sm">
          <div className="px-4 py-3 border-b border-border-subtle">
            <span className="text-[11px] uppercase tracking-wider text-neutral-500">
              Category Breakdown
            </span>
          </div>
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border-subtle text-neutral-500 text-left">
                <th className="py-2 px-4 font-medium">Category</th>
                <th className="py-2 px-4 font-medium text-right">Trades</th>
                <th className="py-2 px-4 font-medium text-right">Volume</th>
              </tr>
            </thead>
            <tbody>
              {profile.categories.map((c) => (
                <tr key={c.category} className="border-b border-border-subtle">
                  <td className="py-2 px-4 text-neutral-300">{c.category}</td>
                  <td className="py-2 px-4 text-right font-mono text-neutral-400">{c.trades}</td>
                  <td className="py-2 px-4 text-right font-mono text-neutral-400">{fmtUsd(c.volume_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Recent Trades */}
      {trades.length > 0 && (
        <div className="bg-surface-1 border border-border-subtle rounded-sm">
          <div className="px-4 py-3 border-b border-border-subtle">
            <span className="text-[11px] uppercase tracking-wider text-neutral-500">
              Recent Trades ({trades.length})
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border-subtle text-neutral-500 text-left">
                  <th className="py-2 px-3 font-medium">Market</th>
                  <th className="py-2 px-3 font-medium">Side</th>
                  <th className="py-2 px-3 font-medium text-right">Price</th>
                  <th className="py-2 px-3 font-medium text-right">Size</th>
                  <th className="py-2 px-3 font-medium text-right">Time</th>
                  <th className="py-2 px-3 font-medium text-right">Outcome</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => (
                  <tr
                    key={t.trade_id}
                    className="border-b border-border-subtle hover:bg-surface-2 transition-colors cursor-pointer"
                    onClick={() => navigate(`/market/${t.market_id}`)}
                  >
                    <td className="py-2 px-3 max-w-[300px] truncate text-neutral-300">
                      {t.market_question
                        ? t.market_question.length > 55
                          ? t.market_question.slice(0, 55) + '…'
                          : t.market_question
                        : t.market_id.slice(0, 12) + '…'}
                    </td>
                    <td className={`py-2 px-3 font-mono font-medium ${sideColor(t.side)}`}>
                      {t.side}
                    </td>
                    <td className="py-2 px-3 text-right font-mono text-neutral-300">
                      {fmtPrice(t.price)}
                    </td>
                    <td className="py-2 px-3 text-right font-mono text-neutral-300">
                      {fmtUsd(t.size_usd)}
                    </td>
                    <td className="py-2 px-3 text-right text-neutral-500">
                      {timeAgo(t.timestamp)}
                    </td>
                    <td className="py-2 px-3 text-right">
                      {t.resolved ? (
                        <span className={`font-mono ${
                          (t.side === 'BUY' && (t.resolved_price ?? 0) >= 0.95) ||
                          (t.side === 'SELL' && (t.resolved_price ?? 1) <= 0.05)
                            ? 'text-emerald-400'
                            : 'text-red-400'
                        }`}>
                          {((t.side === 'BUY' && (t.resolved_price ?? 0) >= 0.95) ||
                            (t.side === 'SELL' && (t.resolved_price ?? 1) <= 0.05))
                            ? 'WIN'
                            : 'LOSS'}
                        </span>
                      ) : (
                        <span className="text-neutral-600">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Wallet Signals */}
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

function StatCard({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="bg-surface-1 border border-border-subtle rounded-sm px-4 py-3">
      <div className="text-[11px] uppercase tracking-wider text-neutral-500 mb-1">{label}</div>
      <div className={`font-mono text-lg font-medium ${accent ? 'text-emerald-400' : 'text-neutral-100'}`}>
        {value}
      </div>
    </div>
  )
}
