import { useEffect, useId } from 'react'
import { motion, useMotionValue, useSpring, useTransform } from 'framer-motion'
import { cn } from '@/lib/cn'
import { clamp01 } from '@/lib/format'

/** Gauge sweep, in degrees, using the automotive convention: 7 o'clock to 5. */
const START_ANGLE = 135
const SWEEP = 270
const RADIUS = 78
const CENTER = 100

/**
 * Polar to SVG coordinates, with y growing downward.
 *
 * No 90-degree offset: angles are measured the way SVG measures them, so
 * START_ANGLE 135 lands at the bottom-left and the sweep runs clockwise
 * through left, top and right to the bottom-right. Offsetting here put the
 * gauge's zero at the bottom-*right* and made every reading fill backwards.
 */
function polar(angleDeg: number, radius: number): { x: number; y: number } {
  const radians = (angleDeg * Math.PI) / 180
  return {
    x: CENTER + radius * Math.cos(radians),
    y: CENTER + radius * Math.sin(radians),
  }
}

function arcPath(from: number, to: number, radius: number): string {
  const start = polar(from, radius)
  const end = polar(to, radius)
  const large = to - from > 180 ? 1 : 0
  return `M ${start.x} ${start.y} A ${radius} ${radius} 0 ${large} 1 ${end.x} ${end.y}`
}

const TRACK = arcPath(START_ANGLE, START_ANGLE + SWEEP, RADIUS)
const ARC_LENGTH = 2 * Math.PI * RADIUS * (SWEEP / 360)

export interface GaugeThreshold {
  /** Fraction of the range, 0..1, at which this colour takes over. */
  at: number
  color: string
}

interface RadialGaugeProps {
  label: string
  value: number | null
  unit: string
  /** Where the value sits in its range, 0..1. */
  fraction: number
  /** Colour bands, lowest first. The last one whose `at` is passed wins. */
  thresholds?: GaugeThreshold[]
  /** Rendered small under the value — a secondary reading or a note. */
  footnote?: string
  warning?: string | null
  size?: number
  className?: string
}

const DEFAULT_THRESHOLDS: GaugeThreshold[] = [
  { at: 0, color: '#10b981' },
  { at: 0.7, color: '#f59e0b' },
  { at: 0.88, color: '#f43f5e' },
]

function colorFor(fraction: number, thresholds: GaugeThreshold[]): string {
  let chosen = thresholds[0]?.color ?? '#38bdf8'
  for (const threshold of thresholds) {
    if (fraction >= threshold.at) chosen = threshold.color
  }
  return chosen
}

/**
 * A circular telemetry gauge with a spring-driven needle.
 *
 * The needle is interpolated with a spring rather than snapped to each frame:
 * telemetry arrives twice a second, and a hard jump reads as a glitch while a
 * settling needle reads as a physical instrument. The glow behind the arc is
 * the "light trail" — it tracks the same spring, so it lags very slightly, the
 * way a real backlit gauge would.
 */
export function RadialGauge({
  label,
  value,
  unit,
  fraction,
  thresholds = DEFAULT_THRESHOLDS,
  footnote,
  warning,
  size = 168,
  className,
}: RadialGaugeProps) {
  const gradientId = useId()
  const glowId = useId()

  const target = useMotionValue(0)
  const spring = useSpring(target, { stiffness: 90, damping: 18, mass: 0.6 })

  useEffect(() => {
    target.set(clamp01(fraction))
  }, [fraction, target])

  const dashOffset = useTransform(spring, (f) => ARC_LENGTH * (1 - f))
  // The needle is drawn pointing up (270 degrees here), so subtract that to
  // turn an absolute gauge angle into a CSS rotation.
  const needleRotation = useTransform(spring, (f) => START_ANGLE + SWEEP * f - 270)

  const color = colorFor(clamp01(fraction), thresholds)
  const display = value === null ? '—' : formatGaugeValue(value)

  return (
    <div className={cn('flex flex-col items-center', className)}>
      <svg
        width={size}
        height={size}
        viewBox="0 0 200 200"
        role="img"
        aria-label={`${label}: ${display}${unit ? ` ${unit}` : ''}`}
        className="overflow-visible"
      >
        <defs>
          <linearGradient id={gradientId} x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor={color} stopOpacity="0.45" />
            <stop offset="100%" stopColor={color} stopOpacity="1" />
          </linearGradient>
          <filter id={glowId} x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="4" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        {/* Unfilled track */}
        <path
          d={TRACK}
          fill="none"
          stroke="rgba(148,163,184,0.18)"
          strokeWidth="10"
          strokeLinecap="round"
        />

        {/* Minor ticks: they give the sweep a sense of scale. */}
        {Array.from({ length: 28 }).map((_, index) => {
          const angle = START_ANGLE + (SWEEP * index) / 27
          const outer = polar(angle, RADIUS - 12)
          const inner = polar(angle, index % 9 === 0 ? RADIUS - 22 : RADIUS - 17)
          return (
            <line
              key={index}
              x1={inner.x}
              y1={inner.y}
              x2={outer.x}
              y2={outer.y}
              stroke="rgba(148,163,184,0.45)"
              strokeWidth={index % 9 === 0 ? 1.6 : 0.9}
              strokeLinecap="round"
            />
          )
        })}

        {/* The filled arc, and a blurred copy behind it for the light trail. */}
        <motion.path
          d={TRACK}
          fill="none"
          stroke={color}
          strokeWidth="14"
          strokeLinecap="round"
          strokeDasharray={ARC_LENGTH}
          style={{ strokeDashoffset: dashOffset, opacity: 0.28 }}
          filter={`url(#${glowId})`}
        />
        <motion.path
          d={TRACK}
          fill="none"
          stroke={`url(#${gradientId})`}
          strokeWidth="9"
          strokeLinecap="round"
          strokeDasharray={ARC_LENGTH}
          style={{ strokeDashoffset: dashOffset }}
        />

        {/* Needle */}
        <motion.g style={{ rotate: needleRotation, originX: '100px', originY: '100px' }}>
          <line
            x1={CENTER}
            y1={CENTER}
            x2={CENTER}
            y2={CENTER - RADIUS + 16}
            stroke={color}
            strokeWidth="2.4"
            strokeLinecap="round"
            filter={`url(#${glowId})`}
          />
        </motion.g>
        <circle cx={CENTER} cy={CENTER} r="6" fill="#0b0f19" stroke={color} strokeWidth="2" />

        <text
          x={CENTER}
          y={CENTER + 34}
          textAnchor="middle"
          className="tnum fill-white"
          style={{ fontSize: 30, fontWeight: 700, letterSpacing: '-0.02em' }}
        >
          {display}
        </text>
        <text
          x={CENTER}
          y={CENTER + 54}
          textAnchor="middle"
          className="fill-slate-400"
          style={{ fontSize: 11, letterSpacing: '0.14em' }}
        >
          {unit.toUpperCase()}
        </text>
      </svg>

      <div className="mt-1 text-center">
        <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-300">
          {label}
        </div>
        {warning ? (
          <div className="mt-1 max-w-[190px] text-[10px] leading-snug text-alert">{warning}</div>
        ) : footnote ? (
          <div className="mt-1 text-[10px] text-slate-500">{footnote}</div>
        ) : null}
      </div>
    </div>
  )
}

function formatGaugeValue(value: number): string {
  const abs = Math.abs(value)
  if (abs >= 10000) return `${(value / 1000).toFixed(1)}k`
  if (abs >= 1000) return Math.round(value).toLocaleString()
  if (abs >= 100) return String(Math.round(value))
  if (abs >= 10) return value.toFixed(1)
  return value.toFixed(2)
}
