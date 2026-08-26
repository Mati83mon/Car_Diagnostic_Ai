import { motion } from 'framer-motion'
import { Activity, RefreshCw, ShieldAlert, ShieldCheck, Cpu } from 'lucide-react'
import { cn } from '@/lib/cn'
import { HEALTH_LABEL } from '@/lib/format'
import { StatusPill } from '@/components/StatusPill'
import type { Dtc, InterfaceInfo, ModuleState } from '@/types/protocol'

const HEALTH_BAR: Record<string, string> = {
  online: 'bg-telemetry shadow-telemetry',
  fault: 'bg-alert shadow-alert',
  offline: 'bg-slate-600',
  unknown: 'bg-slate-700',
}

interface VehicleStatusPanelProps {
  modules: ModuleState[]
  interfaceInfo: InterfaceInfo | null
  connected: boolean
  telemetryStale: boolean
  selectedCode: string | null
  onSelectDtc: (dtc: Dtc, moduleName: string) => void
  onRefresh: () => void
  className?: string
}

/**
 * Module health, and the fault list that drives the 3D view.
 *
 * Clicking a code flies the camera to the component it refers to, which is the
 * whole point of the 3D panel: turning "C0034" into "that wheel, over there".
 */
export function VehicleStatusPanel({
  modules,
  interfaceInfo,
  connected,
  telemetryStale,
  selectedCode,
  onSelectDtc,
  onRefresh,
  className,
}: VehicleStatusPanelProps) {
  const queried = modules.filter((module) => module.health !== 'unknown')
  const faults = queried.filter((module) => module.health === 'fault')

  return (
    <section className={cn('glass flex min-h-0 flex-col', className)}>
      <header className="flex items-center justify-between gap-2 border-b border-white/[0.07] px-4 py-3">
        <div className="flex items-center gap-2">
          <Cpu className="h-3.5 w-3.5 text-neon" />
          <h2 className="panel-title">Vehicle status</h2>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={!connected}
          className={cn(
            'rounded-lg border border-white/10 p-1.5 text-slate-500 transition-colors',
            'hover:border-neon/40 hover:text-neon disabled:opacity-40',
          )}
          aria-label="Re-read fault codes"
          title="Re-read fault codes"
        >
          <RefreshCw className="h-3 w-3" />
        </button>
      </header>

      <div className="fade-bottom min-h-0 flex-1 space-y-2.5 overflow-y-auto px-3 py-3">
        {queried.length === 0 && (
          <p className="px-1 py-6 text-center text-xs text-slate-600">
            {connected ? 'Reading modules…' : 'Not connected.'}
          </p>
        )}

        {queried.map((module) => (
          <ModuleCard
            key={module.name}
            module={module}
            selectedCode={selectedCode}
            onSelectDtc={(dtc) => onSelectDtc(dtc, module.name)}
          />
        ))}
      </div>

      <footer className="space-y-2 border-t border-white/[0.07] px-4 py-3">
        <div className="flex flex-wrap items-center gap-1.5">
          {interfaceInfo && (
            <>
              <StatusPill
                tone={connected ? 'telemetry' : 'alert'}
                label={connected ? 'Link up' : 'Link down'}
                pulse={connected && !telemetryStale}
              />
              {interfaceInfo.write_enabled ? (
                <StatusPill tone="caution" label="Writes armed" />
              ) : (
                <StatusPill tone="neon" label="Read only" />
              )}
            </>
          )}
        </div>

        <dl className="space-y-1 text-[10.5px]">
          <Row
            label="Connection"
            value={
              interfaceInfo
                ? `${interfaceInfo.backend} · ${interfaceInfo.channel}`
                : '—'
            }
          />
          <Row
            label="Bit rate"
            value={interfaceInfo ? `${(interfaceInfo.bitrate / 1000).toFixed(0)} kbit/s` : '—'}
          />
          <Row
            label="Faults"
            value={`${faults.length} module${faults.length === 1 ? '' : 's'}`}
            tone={faults.length > 0 ? 'alert' : 'ok'}
          />
        </dl>

        {interfaceInfo && !interfaceInfo.physical && (
          <p className="flex items-start gap-1.5 rounded-lg border border-caution/30 bg-caution/[0.07] px-2 py-1.5 text-[10px] leading-snug text-caution">
            <Activity className="mt-0.5 h-3 w-3 shrink-0" />
            Simulated vehicle — these readings are synthetic, not from a car.
          </p>
        )}
      </footer>
    </section>
  )
}

function ModuleCard({
  module,
  selectedCode,
  onSelectDtc,
}: {
  module: ModuleState
  selectedCode: string | null
  onSelectDtc: (dtc: Dtc) => void
}) {
  const faulted = module.health === 'fault'

  return (
    <motion.div
      layout
      className={cn(
        'glass-inset overflow-hidden',
        // A tint, not a replacement: `bg-alert/[0.05]` alone would win the
        // tailwind-merge conflict against the inset fill and leave the card
        // effectively transparent.
        faulted && 'border-alert/25 shadow-[inset_0_0_0_9999px_rgba(244,63,94,0.06)]',
      )}
    >
      <div className="flex items-stretch">
        <div className={cn('w-[3px] shrink-0', HEALTH_BAR[module.health])} />
        <div className="min-w-0 flex-1 px-3 py-2">
          <div className="flex items-center justify-between gap-2">
            <span
              className={cn(
                'truncate text-[13px] font-bold tracking-tight',
                faulted ? 'alert-text text-alert' : 'text-slate-100',
              )}
            >
              {module.name}
            </span>
            <span className="tnum shrink-0 font-mono text-[9.5px] text-slate-600">
              {module.address}
            </span>
          </div>

          <p className="truncate text-[10.5px] text-slate-400">{module.description}</p>

          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <span
              className={cn(
                'text-[9.5px] font-semibold uppercase tracking-[0.14em]',
                faulted
                  ? 'text-alert'
                  : module.health === 'online'
                    ? 'text-telemetry'
                    : 'text-slate-500',
              )}
            >
              {HEALTH_LABEL[module.health]}
              {faulted && ` · ${module.dtc_count}`}
            </span>
            {!module.verified && (
              <span
                className="rounded border border-caution/30 px-1 py-px text-[8.5px] uppercase tracking-[0.12em] text-caution/90"
                title="Community-derived address. Silence may mean the address is wrong, not that the module is absent."
              >
                unverified addr
              </span>
            )}
          </div>
        </div>
      </div>

      {module.dtcs.length > 0 && (
        <ul className="border-t border-white/[0.05]">
          {module.dtcs.map((dtc) => {
            const active = selectedCode === dtc.full_code
            return (
              <li key={dtc.full_code}>
                <button
                  type="button"
                  onClick={() => onSelectDtc(dtc)}
                  className={cn(
                    'group flex w-full items-start gap-2 px-3 py-1.5 text-left transition-colors',
                    active ? 'bg-alert/[0.12]' : 'hover:bg-white/[0.04]',
                  )}
                >
                  <span
                    className={cn(
                      'tnum mt-px font-mono text-[11px] font-bold',
                      dtc.status.confirmed ? 'text-alert' : 'text-caution',
                    )}
                  >
                    {dtc.full_code}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[10.5px] leading-snug text-slate-300 group-hover:text-slate-100">
                      {dtc.description}
                    </span>
                    <span className="mt-0.5 flex gap-1.5 text-[8.5px] uppercase tracking-[0.12em]">
                      {dtc.status.confirmed && <span className="text-alert/80">confirmed</span>}
                      {dtc.status.pending && !dtc.status.confirmed && (
                        <span className="text-caution/80">pending</span>
                      )}
                      {!dtc.generic && (
                        <span className="text-slate-600">manufacturer-specific</span>
                      )}
                    </span>
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </motion.div>
  )
}

function Row({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone?: 'alert' | 'ok'
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="text-slate-600">{label}</dt>
      <dd
        className={cn(
          'tnum truncate font-mono',
          tone === 'alert' ? 'text-alert' : tone === 'ok' ? 'text-telemetry' : 'text-slate-400',
        )}
      >
        {value}
      </dd>
    </div>
  )
}

/** Compact safety banner for the header. */
export function SafetyBadge({ info }: { info: InterfaceInfo | null }) {
  if (!info) return null
  const readOnly = !info.write_enabled
  const Icon = readOnly ? ShieldCheck : ShieldAlert
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1',
        'text-[10px] font-semibold uppercase tracking-[0.16em]',
        readOnly
          ? 'border-neon/30 bg-neon/[0.07] text-neon'
          : 'border-caution/40 bg-caution/[0.08] text-caution',
      )}
      title={
        readOnly
          ? 'Writes are disabled. clear_dtc will be refused outright.'
          : 'Writes are enabled. Each one still requires your explicit authorisation.'
      }
    >
      <Icon className="h-3 w-3" />
      {info.safety_mode.replace('_', ' ')}
    </span>
  )
}
