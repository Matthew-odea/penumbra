import {
  ComposedChart,
  Bar,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from 'recharts'
import type { VolumePoint, AnomalyPoint } from '../api/types'

interface Props {
  data: VolumePoint[]
  anomalies?: AnomalyPoint[]
}

export default function VolumeChart({ data, anomalies }: Props) {
  if (data.length === 0) {
    return (
      <div className="h-48 flex items-center justify-center text-neutral-600 text-xs">
        No volume data available for this period.
      </div>
    )
  }

  // Merge volume + anomaly data by hour key
  const anomalyByHour = new Map((anomalies ?? []).map((a) => [a.hour.slice(0, 13), a.z_score]))

  const formatted = data.map((d) => {
    const hourKey = d.hour.slice(0, 13)
    return {
      ...d,
      z_score: anomalyByHour.get(hourKey) ?? null,
      label: new Date(d.hour).toLocaleTimeString('en-US', {
        hour: 'numeric',
        minute: '2-digit',
        hour12: true,
      }),
    }
  })

  const hasAnomalies = anomalies && anomalies.length > 0

  return (
    <div className="h-48">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={formatted} margin={{ top: 4, right: hasAnomalies ? 36 : 4, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e1e1e" vertical={false} />
          <XAxis
            dataKey="label"
            tick={{ fill: '#525252', fontSize: 10 }}
            axisLine={{ stroke: '#1e1e1e' }}
            tickLine={false}
            interval="preserveStartEnd"
          />
          <YAxis
            yAxisId="vol"
            tick={{ fill: '#525252', fontSize: 10 }}
            axisLine={false}
            tickLine={false}
            tickFormatter={(v: number) =>
              v >= 1000 ? `$${(v / 1000).toFixed(0)}k` : `$${v}`
            }
          />
          {hasAnomalies && (
            <YAxis
              yAxisId="z"
              orientation="right"
              tick={{ fill: '#525252', fontSize: 10 }}
              axisLine={false}
              tickLine={false}
              tickFormatter={(v: number) => `z${v.toFixed(1)}`}
              domain={['auto', 'auto']}
            />
          )}
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
              if (name === 'Z-Score') return [value?.toFixed(2), 'Z-Score']
              return [`$${value.toLocaleString()}`, 'Volume']
            }}
          />
          <Bar
            yAxisId="vol"
            dataKey="volume_usd"
            fill="#f59e0b"
            radius={[2, 2, 0, 0]}
            maxBarSize={24}
            name="Volume"
          />
          {hasAnomalies && (
            <Line
              yAxisId="z"
              type="monotone"
              dataKey="z_score"
              stroke="#ef4444"
              strokeWidth={1.5}
              dot={false}
              name="Z-Score"
              connectNulls
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
