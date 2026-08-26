import { useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { BookOpen, Car, ChevronDown, ExternalLink, Globe } from 'lucide-react'
import { cn } from '@/lib/cn'
import type { Citation } from '@/types/protocol'

const KIND = {
  manual: {
    icon: BookOpen,
    label: 'Workshop manual',
    text: 'text-neon',
    border: 'border-neon/25',
    bg: 'bg-neon/[0.06]',
  },
  web: {
    icon: Globe,
    label: 'Web / forum',
    text: 'text-caution',
    border: 'border-caution/25',
    bg: 'bg-caution/[0.06]',
  },
  vehicle: {
    icon: Car,
    label: 'Live vehicle',
    text: 'text-telemetry',
    border: 'border-telemetry/25',
    bg: 'bg-telemetry/[0.06]',
  },
} as const

/**
 * An expandable source card.
 *
 * Colour-coded by authority, matching the agent's own evidence hierarchy:
 * green for what the car actually reported, cyan for the manufacturer's
 * procedure, amber for the internet. A mechanic should be able to tell at a
 * glance whether a claim came from a measurement or from a forum post.
 */
export function CitationCard({ citation }: { citation: Citation }) {
  const [open, setOpen] = useState(false)
  const kind = KIND[citation.kind]
  const Icon = kind.icon
  const hasDetail = citation.detail.trim().length > 0

  return (
    <div className={cn('overflow-hidden rounded-lg border', kind.border, kind.bg)}>
      <button
        type="button"
        onClick={() => hasDetail && setOpen((value) => !value)}
        className={cn(
          'flex w-full items-center gap-2 px-2.5 py-1.5 text-left transition-colors',
          hasDetail && 'hover:bg-white/[0.04]',
        )}
        aria-expanded={open}
      >
        <Icon className={cn('h-3.5 w-3.5 shrink-0', kind.text)} />
        <span className="min-w-0 flex-1">
          <span className={cn('block text-[9px] font-semibold uppercase tracking-[0.16em]', kind.text)}>
            {kind.label}
            {citation.score !== null && (
              <span className="ml-1.5 tnum opacity-60">{citation.score.toFixed(2)}</span>
            )}
          </span>
          <span className="block truncate text-[11px] text-slate-300">{citation.label}</span>
        </span>
        {citation.url && (
          <a
            href={citation.url}
            target="_blank"
            rel="noreferrer noopener"
            onClick={(event) => event.stopPropagation()}
            className="shrink-0 rounded p-1 text-slate-500 transition-colors hover:text-slate-200"
            aria-label="Open source in a new tab"
          >
            <ExternalLink className="h-3 w-3" />
          </a>
        )}
        {hasDetail && (
          <ChevronDown
            className={cn(
              'h-3.5 w-3.5 shrink-0 text-slate-500 transition-transform',
              open && 'rotate-180',
            )}
          />
        )}
      </button>

      <AnimatePresence initial={false}>
        {open && hasDetail && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.22, ease: 'easeOut' }}
            className="overflow-hidden"
          >
            <p className="whitespace-pre-wrap border-t border-white/[0.06] px-2.5 py-2 font-mono text-[10.5px] leading-relaxed text-slate-400">
              {citation.detail}
            </p>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
