import { Outlet, NavLink, useLocation } from 'react-router-dom'
import { useBudget, useHealth } from '../hooks/queries'

export default function Layout() {
  const { data: health } = useHealth()
  const { data: budget } = useBudget()
  const location = useLocation()

  const isOnline = health?.status === 'ok'

  const t1Used = budget?.tier1.calls_used ?? 0
  const t1Limit = budget?.tier1.calls_limit ?? 200
  const t2Used = budget?.tier2.calls_used ?? 0
  const t2Limit = budget?.tier2.calls_limit ?? 30

  return (
    <div className="min-h-screen flex flex-col">
      {/* ── Top Bar ────────────────────────────────────────────────── */}
      <header className="h-12 flex items-center justify-between px-5 border-b border-border-subtle bg-surface-1 shrink-0">
        <div className="flex items-center gap-6">
          {/* Brand */}
          <NavLink to="/" className="flex items-center gap-2 text-sm font-semibold text-neutral-100 tracking-tight">
            <span className="text-accent">◆</span>
            Penumbra
          </NavLink>

          {/* Nav */}
          <nav className="flex items-center gap-1">
            <NavItem to="/" label="Feed" active={location.pathname === '/'} />
            <NavItem to="/metrics" label="Metrics" active={location.pathname === '/metrics'} />
          </nav>
        </div>

        {/* Right: Budget + Status */}
        <div className="flex items-center gap-4 text-xs">
          <div className="flex items-center gap-3 font-mono text-neutral-500">
            <span>T1 {t1Used}/{t1Limit}</span>
            <span className="text-border">|</span>
            <span>T2 {t2Used}/{t2Limit}</span>
          </div>

          <div className="flex items-center gap-1.5">
            <div className={`w-1.5 h-1.5 rounded-full ${isOnline ? 'bg-emerald-500' : 'bg-red-500'}`} />
            <span className="text-neutral-500 text-xs">
              {isOnline ? 'Online' : 'Offline'}
            </span>
          </div>
        </div>
      </header>

      {/* ── Content ────────────────────────────────────────────────── */}
      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  )
}

function NavItem({ to, label, active }: { to: string; label: string; active: boolean }) {
  return (
    <NavLink
      to={to}
      className={`px-3 py-1.5 text-xs font-medium rounded-sm transition-colors ${
        active
          ? 'bg-surface-3 text-neutral-100'
          : 'text-neutral-500 hover:text-neutral-300 hover:bg-surface-2'
      }`}
    >
      {label}
    </NavLink>
  )
}
