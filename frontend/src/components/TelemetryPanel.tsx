import { useMemo } from 'react'
import { Area, AreaChart, ResponsiveContainer, YAxis } from 'recharts'
import { AlertTriangle, Gauge } from 'lucide-react'
import { cn } from '@/lib/cn'
import { formatValue, fraction, labelFor, numeric } from '@/lib/format'
import { RadialGauge, type GaugeThreshold } from '@/components/RadialGauge'
import type { SignalReading } from '@/types/protocol'
import type { TelemetryPoint } from '@/hooks/useDiagnostics'

/** The three signals promoted to full gauges. */
const PRIMARY: { signal: string; thresholds?: GaugeThreshold[] }[] = [
  {
    signal: 'THROTTLE_POS',
    thresholds: [
      { at: 0, color: '#10b981' },
      { at: 0.65, color: '#f59e0b' },
      { at: 0.9, color: '#f43f5e' },
    ],
  },
  {
    signal: 'RPM',
    thresholds: [
      { at: 0, color: '#38bdf8' },
      { at: 0.72, color: '#f59e0b' },
      { at: 0.88, color: '#f43f5e' },
    ],
  },
  {
    // Battery is the inverse case: low is the problem, not high.
    signal: 'MODULE_VOLTAGE',
    thresholds: [
      { at: 0, color: '#f43f5e' },
      { at: 0.35, color: '#f59e0b' },
      { at: 0.52, color: '#10b981' },
      { at: 0.86, color: '#f59e0b' },
    ],
  },
]

const SECONDARY = [
  'COOLANT_TEMP',
  'MAP',
  'BAROMETRIC_PRESSURE',
  'MAF',
  'ENGINE_LOAD',
  'INTAKE_TEMP',
  'FUEL_RAIL_PRESSURE',
  'OIL_TEMP',
]

interface TelemetryPanelProps {
  readings: Record<string, SignalReading>
  history: TelemetryPoint[]
  stale: boolean
  className?: string
}

export function TelemetryPanel({ readings, history, stale, className }: TelemetryPanelProps) {
  const primary = PRIMARY[0]
  const primaryReading = readings[primary.signal]

  const chartData = useMemo(
    () => history.map((point) => ({ t: point.t, rpm: point.RPM ?? 0, map: point.MAP ?? 0 })),
    [history],
  )

  return (
    <section className={cn('glass flex min-h-0 flex-col', className)}>
      <header className="flex items-center justify-between gap-2 border-b border-white/[0.07] px-4 py-3">
        <div className="flex items-center gap-2">
          <Gauge className="h-3.5 w-3.5 text-neon" />
          <h2 className="panel-title">Telemetry</h2>
        </div>
        {stale && (
          <span className="flex items-center gap-1 text-[9.5px] font-semibold uppercase tracking-[0.14em] text-caution">
            <AlertTriangle className="h-3 w-3" />
            stale
          </span>
        )}
      </header>

      {/* max-w keeps the gauges as a group when the panel spans the full
          width on tablet; without it they drift to opposite edges and stop
          reading as one instrument cluster. */}
      <div
        className={cn(
          'mx-auto w-full max-w-[460px] xl:max-w-none',
          'min-h-0 flex-1 overflow-y-auto',
          stale && 'opacity-60',
        )}
      >
        {/* Hero gauge */}
        <div className="flex justify-center px-4 pt-4">
          <RadialGauge
            label={labelFor(primary.signal)}
            value={numeric(primaryReading)}
            unit={primaryReading?.unit ?? '%'}
            fraction={fraction(primary.signal, numeric(primaryReading))}
            thresholds={primary.thresholds}
            warning={primaryReading?.warning ?? null}
            size={172}
          />
        </div>

        {/* Two smaller gauges */}
        <div className="grid grid-cols-2 gap-2 px-3 pt-2">
          {PRIMARY.slice(1).map((entry) => {
            const reading = readings[entry.signal]
            return (
              <RadialGauge
                key={entry.signal}
                label={labelFor(entry.signal)}
                value={numeric(reading)}
                unit={reading?.unit ?? ''}
                fraction={fraction(entry.signal, numeric(reading))}
                thresholds={entry.thresholds}
                warning={reading?.warning ?? null}
                size={118}
              />
            )
          })}
        </div>

        {/* Live trace */}
        {chartData.length > 3 && (
          <div className="px-4 pt-3">
            <div className="panel-title mb-1.5">Engine speed · last 60s</div>
            <div className="glass-inset h-[62px] px-1 py-1">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 4, right: 2, bottom: 0, left: 2 }}>
                  <defs>
                    <linearGradient id="rpmFill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#38bdf8" stopOpacity={0.42} />
                      <stop offset="100%" stopColor="#38bdf8" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  {/* A padded domain keeps a steady idle from rendering as a
                      bare line pinned to the top of an empty box. */}
                  <YAxis hide domain={['dataMin - 120', 'dataMax + 120']} />
                  <Area
                    type="monotone"
                    dataKey="rpm"
                    stroke="#38bdf8"
                    strokeWidth={1.8}
                    fill="url(#rpmFill)"
                    dot={false}
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        {/* Everything else */}
        <div className="px-3 pb-4 pt-3">
          <div className="panel-title mb-1.5 px-1">Signals</div>
          <div className="grid grid-cols-2 gap-1.5">
            {SECONDARY.map((signal) => {
              const reading = readings[signal]
              const value = numeric(reading)
              return (
                <div
                  key={signal}
                  className={cn(
                    'glass-inset px-2.5 py-1.5',
                    reading?.warning && 'border-alert/40 bg-alert/[0.06]',
                  )}
                  title={reading?.warning ?? reading?.description ?? undefined}
                >
                  <div className="truncate text-[9px] font-semibold uppercase tracking-[0.14em] text-slate-500">
                    {labelFor(signal)}
                  </div>
                  <div
                    className={cn(
                      'tnum mt-0.5 truncate font-mono text-[12.5px] font-semibold',
                      reading?.warning ? 'text-alert' : 'text-slate-200',
                    )}
                  >
                    {formatValue(value, reading?.unit ?? '')}
                  </div>
                  {reading && !reading.verified_scaling && (
                    <div className="text-[8.5px] uppercase tracking-[0.1em] text-caution/80">
                      unverified
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </section>
  )
}
