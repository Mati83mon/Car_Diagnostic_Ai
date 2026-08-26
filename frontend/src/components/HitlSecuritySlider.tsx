import { useCallback, useEffect, useRef, useState } from 'react'
import {
  motion,
  useMotionValue,
  useMotionValueEvent,
  useTransform,
} from 'framer-motion'
import { AlertTriangle, ChevronsRight, Lock, ShieldCheck, X } from 'lucide-react'
import { cn } from '@/lib/cn'
import type { ApprovalRequestFrame } from '@/types/protocol'

/** Fraction of the track that must be crossed to authorise. */
const UNLOCK_THRESHOLD = 0.86
const HANDLE_SIZE = 52
const TRACK_PADDING = 4

const RISK_STYLE = {
  low: { label: 'LOW RISK', text: 'text-caution', border: 'border-caution/40' },
  medium: { label: 'MEDIUM RISK', text: 'text-caution', border: 'border-caution/50' },
  high: { label: 'HIGH RISK', text: 'text-alert', border: 'border-alert/60' },
} as const

interface HitlSecuritySliderProps {
  request: ApprovalRequestFrame
  onAuthorize: () => void
  onDecline: () => void
}

/**
 * The physical authorisation gesture for a vehicle write.
 *
 * A deliberate, sustained drag rather than a button, for one reason: a button
 * can be hit by accident or by muscle memory, and this action erases the
 * freeze-frame evidence of a fault permanently. The gesture has to be
 * *intended*.
 *
 * The gate itself does not live here. This component sends one boolean; the
 * credential that actually authorises the write is held server-side and never
 * reaches the browser. If this component had a bug, the worst it could do is
 * fail to ask.
 */
export function HitlSecuritySlider({
  request,
  onAuthorize,
  onDecline,
}: HitlSecuritySliderProps) {
  const trackRef = useRef<HTMLDivElement>(null)
  const [trackWidth, setTrackWidth] = useState(0)
  const [unlocked, setUnlocked] = useState(false)
  const [dragging, setDragging] = useState(false)
  const x = useMotionValue(0)

  const maxDrag = Math.max(trackWidth - HANDLE_SIZE - TRACK_PADDING * 2, 1)
  const progress = useTransform(x, [0, maxDrag], [0, 1])

  // The fill and the glow both track the drag, so the control feels loaded
  // rather than merely moved.
  const fillWidth = useTransform(progress, (p) => `${Math.max(p * 100, 0)}%`)
  const glowOpacity = useTransform(progress, [0, 0.5, 1], [0.12, 0.4, 0.95])
  const promptOpacity = useTransform(progress, [0, 0.45], [1, 0])

  const [nearThreshold, setNearThreshold] = useState(false)
  useMotionValueEvent(progress, 'change', (value) => {
    setNearThreshold(value >= UNLOCK_THRESHOLD)
  })

  useEffect(() => {
    const element = trackRef.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => {
      setTrackWidth(entry.contentRect.width)
    })
    observer.observe(element)
    setTrackWidth(element.clientWidth)
    return () => observer.disconnect()
  }, [])

  // A fresh request must never inherit the previous one's drag position.
  useEffect(() => {
    setUnlocked(false)
    x.set(0)
  }, [request.approval_id, x])

  const handleDragEnd = useCallback(() => {
    setDragging(false)
    if (progress.get() >= UNLOCK_THRESHOLD) {
      setUnlocked(true)
      x.set(maxDrag)
      // Let the confirmation flash land before the panel disappears.
      window.setTimeout(onAuthorize, 420)
    } else {
      // Elastic return: the spring makes an incomplete drag feel rejected
      // rather than merely undone.
      x.set(0)
    }
  }, [maxDrag, onAuthorize, progress, x])

  const risk = RISK_STYLE[request.risk] ?? RISK_STYLE.medium

  return (
    // A plain element on purpose. When this was a `motion.div` with its own
    // `exit`, the nested exit never resolved -- the overlay animated to
    // opacity 0 but stayed mounted, leaving an invisible `fixed inset-0`
    // sheet swallowing every click in the app. The overlay above owns the
    // animation; this card just renders.
    <div
      className={cn(
        'glass flex max-h-[86vh] w-full max-w-2xl flex-col overflow-hidden border-alert/40',
        'shadow-[0_0_60px_-10px_rgba(244,63,94,0.6)]',
      )}
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="hitl-title"
    >
      {/* Header */}
      <div className="flex shrink-0 items-center justify-between gap-3 border-b border-alert/20 bg-alert/[0.07] px-4 py-3">
        <div className="flex items-center gap-2.5">
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-sonar rounded-full bg-alert/50" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-alert" />
          </span>
          <h3
            id="hitl-title"
            className="alert-text text-xs font-bold uppercase tracking-[0.2em] text-alert"
          >
            Write operation · approval required
          </h3>
        </div>
        <span
          className={cn(
            'rounded border px-2 py-0.5 text-[10px] font-bold uppercase tracking-[0.14em]',
            risk.text,
            risk.border,
          )}
        >
          {risk.label}
        </span>
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        {/* What, where, and whether it can be undone */}
        <div className="grid grid-cols-3 gap-2 text-[11px]">
          <Field label="Operation" value={request.tool} mono />
          <Field label="Module" value={request.module} mono />
          <Field
            label="Reversible"
            value={request.reversible ? 'yes' : 'NO'}
            tone={request.reversible ? undefined : 'alert'}
          />
        </div>

        <div className="glass-inset px-3 py-2">
          <div className="panel-title mb-1.5">Scope</div>
          <p className="text-xs text-slate-300">{request.scope}</p>
        </div>

        {!request.address_verified && (
          <div className="flex items-start gap-2 rounded-lg border border-caution/40 bg-caution/[0.08] px-3 py-2">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-caution" />
            <p className="text-[11px] leading-snug text-caution">
              This module's address is <strong>unverified</strong> for this vehicle. If it is
              wrong, this write reaches a different module than you intend.
            </p>
          </div>
        )}

        {/* Exactly what would be erased */}
        {request.affected_codes.length > 0 && (
          <div className="glass-inset overflow-hidden">
            <div className="panel-title border-b border-white/[0.05] px-3 py-2">
              Will be erased · {request.affected_codes.length}
            </div>
            <ul className="max-h-32 divide-y divide-white/[0.04] overflow-y-auto">
              {request.affected_codes.map((dtc) => (
                <li key={dtc.full_code} className="flex gap-3 px-3 py-1.5">
                  <span className="tnum font-mono text-[11px] font-bold text-alert">
                    {dtc.full_code}
                  </span>
                  <span className="truncate text-[11px] text-slate-400">{dtc.description}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Consequences, verbatim from the server */}
        {request.risks.length > 0 && (
          <ul className="space-y-1.5">
            {request.risks.map((item, index) => (
              <li key={index} className="flex items-start gap-2 text-[11px] leading-snug text-slate-400">
                <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-alert/70" />
                {item}
              </li>
            ))}
          </ul>
        )}

        {/* The gesture */}
        <div
          ref={trackRef}
          className={cn(
            'relative mt-1 h-[60px] select-none overflow-hidden rounded-xl border transition-colors',
            unlocked
              ? 'border-telemetry/60 bg-telemetry/10'
              : nearThreshold
                ? 'border-alert/70 bg-alert/[0.10]'
                : 'border-white/10 bg-black/35',
          )}
        >
          {/* Progress fill */}
          <motion.div
            className={cn(
              'absolute inset-y-0 left-0',
              unlocked ? 'bg-telemetry/25' : 'bg-alert/20',
            )}
            style={{ width: fillWidth }}
          />
          {/* Glow that intensifies with the drag */}
          <motion.div
            aria-hidden
            className="pointer-events-none absolute inset-0"
            style={{
              opacity: glowOpacity,
              boxShadow: unlocked
                ? 'inset 0 0 42px rgba(16,185,129,0.55)'
                : 'inset 0 0 42px rgba(244,63,94,0.55)',
            }}
          />

          {/* Prompt, fading out as the handle advances */}
          <motion.div
            style={{ opacity: unlocked ? 0 : promptOpacity }}
            className="pointer-events-none absolute inset-0 flex items-center justify-center gap-2 pl-12"
          >
            <ChevronsRight className="h-4 w-4 animate-pulse text-alert/70" />
            <span className="text-[11px] font-bold uppercase tracking-[0.22em] text-alert/90">
              Slide to authorize
            </span>
          </motion.div>

          {unlocked && (
            <motion.div
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              className="pointer-events-none absolute inset-0 flex items-center justify-center gap-2"
            >
              <ShieldCheck className="h-4 w-4 text-telemetry" />
              <span className="text-[11px] font-bold uppercase tracking-[0.22em] text-telemetry">
                Authorized
              </span>
            </motion.div>
          )}

          {/* Handle */}
          <motion.button
            type="button"
            drag={unlocked ? false : 'x'}
            dragConstraints={{ left: 0, right: maxDrag }}
            dragElastic={0.06}
            dragMomentum={false}
            onDragStart={() => setDragging(true)}
            onDragEnd={handleDragEnd}
            style={{ x, width: HANDLE_SIZE, height: HANDLE_SIZE, top: TRACK_PADDING, left: TRACK_PADDING }}
            animate={unlocked ? { x: maxDrag } : undefined}
            transition={{ type: 'spring', stiffness: 420, damping: 32 }}
            whileTap={{ scale: 0.95 }}
            aria-label="Drag right to authorise this write operation"
            className={cn(
              'absolute flex cursor-grab items-center justify-center rounded-lg border active:cursor-grabbing',
              'touch-none outline-none focus-visible:ring-2 focus-visible:ring-neon/70',
              unlocked
                ? 'border-telemetry/70 bg-telemetry/25 text-telemetry'
                : 'border-alert/50 bg-[#1a0f18] text-alert shadow-[0_0_18px_rgba(244,63,94,0.35)]',
            )}
          >
            {unlocked ? (
              <ShieldCheck className="h-5 w-5" />
            ) : (
              <Lock className={cn('h-5 w-5 transition-transform', dragging && 'scale-90')} />
            )}
          </motion.button>
        </div>

        <div className="flex items-center justify-between pt-0.5">
          <button
            type="button"
            onClick={onDecline}
            disabled={unlocked}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-lg border border-white/10 px-3 py-1.5',
              'text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-400',
              'transition-colors hover:border-white/20 hover:text-slate-200',
              'disabled:opacity-40',
            )}
          >
            <X className="h-3.5 w-3.5" />
            Decline
          </button>
          <span className="text-[10px] text-slate-500">
            Anything other than a completed slide is a refusal.
          </span>
        </div>
      </div>
    </div>
  )
}

function Field({
  label,
  value,
  mono,
  tone,
}: {
  label: string
  value: string
  mono?: boolean
  tone?: 'alert'
}) {
  return (
    <div className="glass-inset px-2.5 py-1.5">
      <div className="text-[9px] font-semibold uppercase tracking-[0.16em] text-slate-500">
        {label}
      </div>
      <div
        className={cn(
          'mt-0.5 truncate text-[11px] font-semibold',
          mono && 'font-mono',
          tone === 'alert' ? 'text-alert' : 'text-slate-200',
        )}
      >
        {value}
      </div>
    </div>
  )
}

/**
 * The approval overlay.
 *
 * A modal, not an inline panel. Two reasons, both found by testing: an inline
 * card competing for space in a column layout gets clipped, and the slider --
 * the one control that matters -- is the part that disappears. And an
 * irreversible write to a vehicle deserves the screen's full attention.
 *
 * Mounted by a plain conditional, deliberately *not* by `AnimatePresence`.
 * With an exit animation the overlay reliably faded to `opacity: 0` and then
 * stayed in the DOM, leaving an invisible `fixed inset-0` sheet that swallowed
 * every click in the application. A 180 ms fade-out is not worth a failure
 * mode that bricks the UI, so the entrance is a CSS animation and the exit is
 * an unmount.
 *
 * The backdrop is inert: clicking outside does not dismiss it, because a stray
 * click must not read as a decision in either direction. Escape declines --
 * the safe direction, and the one users expect.
 */
export function HitlApprovalGate({
  request,
  onAuthorize,
  onDecline,
}: {
  request: ApprovalRequestFrame | null
  onAuthorize: (id: string) => void
  onDecline: (id: string) => void
}) {
  useEffect(() => {
    if (!request) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onDecline(request.approval_id)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [request, onDecline])

  if (!request) return null

  return (
    <div
      key={request.approval_id}
      className="fixed inset-0 z-[60] flex animate-rise items-center justify-center p-4 sm:p-6"
    >
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" />
      <div className="relative flex w-full justify-center">
        <HitlSecuritySlider
          request={request}
          onAuthorize={() => onAuthorize(request.approval_id)}
          onDecline={() => onDecline(request.approval_id)}
        />
      </div>
    </div>
  )
}
