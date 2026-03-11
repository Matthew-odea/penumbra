import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import type { VolumePoint } from '../api/types'

interface Props {
  data: VolumePoint[]
}

export default function VolumeChart({ data }: Props) {
  if (data.length === 0) {
    return (
      <div className="h-48 flex items-center justify-center text-neutral-600 text-xs">
        No volume data available for this period.
      </div>
    )
  }

  const formatted = data.map((d) => ({
    ...d,
    label: new Date(d.hour).toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    }),
  }))

  return (
    <div className="h-48">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={formatted} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
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
            tickFormatter={(v: number) =>
              v >= 1000 ? `$${(v / 1000).toFixed(0)}k` : `$${v}`
            }
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
            formatter={(value: number) => [`$${value.toLocaleString()}`, 'Volume']}
          />
          <Bar
            dataKey="volume_usd"
            fill="#f59e0b"
            radius={[2, 2, 0, 0]}
            maxBarSize={24}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
