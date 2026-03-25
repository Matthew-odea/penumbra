/* ── Formatting utilities ─────────────────────────────────────────── */

/** Format USD amount: $1,234.56 or $1.2M for large numbers */
export function fmtUsd(value: number | null | undefined): string {
  if (value == null) return '—'
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `$${value.toLocaleString('en-US', { maximumFractionDigits: 0 })}`
  return `$${value.toFixed(2)}`
}

/** Format number with commas */
export function fmtNum(value: number | null | undefined): string {
  if (value == null) return '—'
  return value.toLocaleString('en-US')
}

/** Format percentage: 72.3% */
export function fmtPct(value: number | null | undefined): string {
  if (value == null) return '—'
  return `${(value * 100).toFixed(1)}%`
}

/** Truncate wallet address: 0xAbCd...7890 */
export function truncAddr(address: string, chars = 4): string {
  if (address.length <= chars * 2 + 2) return address
  return `${address.slice(0, chars + 2)}...${address.slice(-chars)}`
}

/** Relative timestamp: "4m ago", "2h ago", "3d ago" */
export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return '—'
  const now = Date.now()
  const then = new Date(iso).getTime()
  const diff = Math.max(0, now - then)

  const seconds = Math.floor(diff / 1000)
  if (seconds < 60) return `${seconds}s ago`

  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`

  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`

  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

/** Format price as probability: 0.65 → "65¢" */
export function fmtPrice(price: number | null | undefined): string {
  if (price == null) return '—'
  return `${Math.round(price * 100)}¢`
}

/** Score → color class */
export function scoreColor(score: number | null | undefined): string {
  if (score == null) return 'text-neutral-500'
  if (score >= 80) return 'text-red-400'
  if (score >= 60) return 'text-orange-400'
  if (score >= 40) return 'text-amber-400'
  return 'text-neutral-400'
}

/** Score → background color class for badges */
export function scoreBg(score: number | null | undefined): string {
  if (score == null) return 'bg-neutral-800'
  if (score >= 80) return 'bg-red-950 text-red-300'
  if (score >= 60) return 'bg-orange-950 text-orange-300'
  if (score >= 40) return 'bg-amber-950 text-amber-300'
  return 'bg-neutral-800 text-neutral-400'
}

/** Side → color class */
export function sideColor(side: string): string {
  return side === 'BUY' ? 'text-emerald-400' : 'text-red-400'
}
