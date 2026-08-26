import { cn } from '@/lib/cn'

type Tone = 'neon' | 'alert' | 'telemetry' | 'caution' | 'muted'

const TONE: Record<Tone, { dot: string; ring: string; text: string; border: string }> = {
  neon: {
    dot: 'bg-neon',
    ring: 'bg-neon/40',
    text: 'text-neon',
    border: 'border-neon/30',
  },
  alert: {
    dot: 'bg-alert',
    ring: 'bg-alert/40',
    text: 'text-alert',
    border: 'border-alert/30',
  },
  telemetry: {
    dot: 'bg-telemetry',
    ring: 'bg-telemetry/40',
    text: 'text-telemetry',
    border: 'border-telemetry/30',
  },
  caution: {
    dot: 'bg-caution',
    ring: 'bg-caution/40',
    text: 'text-caution',
    border: 'border-caution/30',
  },
  muted: {
    dot: 'bg-slate-500',
    ring: 'bg-slate-500/30',
    text: 'text-slate-400',
    border: 'border-white/10',
  },
}

interface StatusPillProps {
  tone?: Tone
  label: string
  /** Emit the expanding sonar ring. Reserve it for genuinely live states. */
  pulse?: boolean
  className?: string
}

/**
 * A small labelled status light.
 *
 * The sonar ring is deliberately opt-in: if everything pulses, nothing reads
 * as urgent.
 */
export function StatusPill({ tone = 'muted', label, pulse = false, className }: StatusPillProps) {
  const colors = TONE[tone]
  return (
    <span
      className={cn(
        'inline-flex items-center gap-2 rounded-full border bg-black/20 px-2.5 py-1',
        'text-[10px] font-semibold uppercase tracking-[0.16em]',
        colors.border,
        colors.text,
        className,
      )}
    >
      <span className="relative flex h-1.5 w-1.5">
        {pulse && (
          <span
            className={cn('absolute inline-flex h-full w-full rounded-full animate-sonar', colors.ring)}
          />
        )}
        <span className={cn('relative inline-flex h-1.5 w-1.5 rounded-full', colors.dot)} />
      </span>
      {label}
    </span>
  )
}
