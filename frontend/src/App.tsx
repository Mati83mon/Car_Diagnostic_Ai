import { useCallback, useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { AlertOctagon, Radio, RotateCcw, X } from 'lucide-react'
import { cn } from '@/lib/cn'
import { useDiagnostics } from '@/hooks/useDiagnostics'
import { GridBackdrop } from '@/components/GridBackdrop'
import { StatusPill } from '@/components/StatusPill'
import { MasterAgentChat } from '@/components/MasterAgentChat'
import { TelemetryPanel } from '@/components/TelemetryPanel'
import { HitlApprovalGate } from '@/components/HitlSecuritySlider'
import { SafetyBadge, VehicleStatusPanel } from '@/components/VehicleStatusPanel'
import { Vehicle3DViewer } from '@/components/vehicle/Vehicle3DViewer'
import { focusPointFor, MODULE_ANCHORS, type FocusPoint } from '@/components/vehicle/geometry'
import type { Dtc } from '@/types/protocol'

export default function App() {
  const diagnostics = useDiagnostics()
  const [focus, setFocus] = useState<FocusPoint | null>(null)
  const [selectedCode, setSelectedCode] = useState<string | null>(null)

  /** Clicking a fault flies the camera to the component it refers to. */
  const handleSelectDtc = useCallback((dtc: Dtc, moduleName: string) => {
    setSelectedCode((current) => {
      if (current === dtc.full_code) {
        setFocus(null)
        return null
      }
      setFocus(focusPointFor(dtc.code, moduleName))
      return dtc.full_code
    })
  }, [])

  const handleSelectModule = useCallback((moduleName: string) => {
    const anchor = MODULE_ANCHORS.find((entry) => entry.module === moduleName)
    if (!anchor) return
    setSelectedCode(null)
    setFocus({ position: anchor.position, label: anchor.label, where: anchor.where })
  }, [])

  const clearFocus = useCallback(() => {
    setFocus(null)
    setSelectedCode(null)
  }, [])

  // Escape returns to the chassis overview — the standard "get me out" gesture.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') clearFocus()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [clearFocus])

  const {
    connected,
    connecting,
    connectionError,
    interfaceInfo,
    vehicle,
    version,
    modules,
    totalDtcs,
    readings,
    telemetryStale,
    history,
    chat,
    tools,
    agentState,
    agentDetail,
    pendingApproval,
    lastError,
    sendChat,
    respondApproval,
    refresh,
    dismissError,
  } = diagnostics

  return (
    <div className="relative flex h-full flex-col overflow-hidden">
      <GridBackdrop />

      {/* ---- header ---- */}
      <header className="relative z-10 shrink-0 border-b border-white/[0.07] bg-black/25 backdrop-blur-xl">
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-2.5 sm:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-neon/35 bg-neon/10 shadow-neon">
              <Radio className="h-4 w-4 text-neon" />
            </div>
            <div className="min-w-0">
              <h1 className="neon-text truncate text-sm font-bold uppercase tracking-[0.24em] text-neon">
                Majster-AI
              </h1>
              <p className="truncate text-[10px] uppercase tracking-[0.14em] text-slate-500">
                {vehicle || 'Diagnostic HUD'}
                {version && ` · v${version}`}
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <StatusPill
              tone={connected ? 'telemetry' : connecting ? 'caution' : 'alert'}
              label={connected ? 'Link established' : connecting ? 'Connecting' : 'Offline'}
              pulse={connected}
            />
            {totalDtcs > 0 && (
              <StatusPill tone="alert" label={`${totalDtcs} DTC`} pulse />
            )}
            <SafetyBadge info={interfaceInfo} />
            {focus && (
              <button
                type="button"
                onClick={clearFocus}
                className={cn(
                  'inline-flex items-center gap-1.5 rounded-lg border border-white/10 px-2.5 py-1',
                  'text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-400',
                  'transition-colors hover:border-neon/40 hover:text-neon',
                )}
              >
                <RotateCcw className="h-3 w-3" />
                Overview
              </button>
            )}
          </div>
        </div>

        <AnimatePresence>
          {connectionError && !connected && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              className="overflow-hidden bg-alert/10"
            >
              <p className="px-4 py-1.5 text-center text-[11px] text-alert sm:px-6">
                {connectionError}
              </p>
            </motion.div>
          )}
        </AnimatePresence>
      </header>

      {/* ---- main grid ---- */}
      <main
        className={cn(
          'relative z-10 min-h-0 flex-1 gap-3 overflow-y-auto p-3 sm:p-4',
          // Mobile: one column. Tablet landscape: two. Desktop: three.
          'grid grid-cols-1 lg:grid-cols-[290px_minmax(0,1fr)] xl:grid-cols-[300px_minmax(0,1fr)_330px]',
          // Explicit placement on tablet. Left to auto-flow, the centre
          // column's content overflowed its row and painted on top of the row
          // below it; naming the cells removes the ambiguity.
          'lg:grid-rows-[auto_auto] xl:grid-rows-1',
          // Only the three-column desktop layout is a fixed-height dashboard
          // where each panel scrolls internally. Below that there simply is
          // not enough height, and clipping the module list mid-row looks
          // broken rather than dense -- so the page scrolls instead.
          'xl:overflow-hidden',
        )}
      >
        <VehicleStatusPanel
          modules={modules}
          interfaceInfo={interfaceInfo}
          connected={connected}
          telemetryStale={telemetryStale}
          selectedCode={selectedCode}
          onSelectDtc={handleSelectDtc}
          onRefresh={refresh}
          className="order-2 max-h-[460px] lg:order-1 lg:col-start-1 lg:row-start-1 lg:max-h-[420px] xl:max-h-none"
        />

        {/* centre: 3D over the terminal */}
        <div className="order-1 flex min-h-0 flex-col gap-3 lg:order-2 lg:col-start-2 lg:row-start-1 lg:row-span-2">
          <div className="glass h-[280px] shrink-0 overflow-hidden p-0 sm:h-[340px] xl:h-[46%] xl:min-h-[280px]">
            <Vehicle3DViewer
              modules={modules}
              focus={focus}
              onSelectModule={handleSelectModule}
            />
          </div>

          <MasterAgentChat
            chat={chat}
            tools={tools}
            agentState={agentState}
            agentDetail={agentDetail}
            connected={connected}
            onSend={sendChat}
            className="min-h-[300px] flex-1 lg:min-h-[360px]"
          />
        </div>

        <TelemetryPanel
          readings={readings}
          history={history}
          stale={telemetryStale}
          className="order-3 lg:col-start-1 lg:row-start-2 lg:max-h-[420px] xl:row-start-1 xl:col-start-3 xl:max-h-none"
        />
      </main>

      {/* ---- approval gate: a fixed overlay, so the column layout can never
              clip the one control that matters ---- */}
      <HitlApprovalGate
        request={pendingApproval}
        onAuthorize={(id) => respondApproval(id, true)}
        onDecline={(id) => respondApproval(id, false)}
      />

      {/* ---- transient errors ---- */}
      <AnimatePresence>
        {lastError && (
          <motion.div
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 16 }}
            className="fixed bottom-4 left-1/2 z-50 w-[min(92vw,460px)] -translate-x-1/2"
          >
            <div className="glass flex items-start gap-2.5 border-alert/30 px-3.5 py-2.5">
              <AlertOctagon className="mt-0.5 h-4 w-4 shrink-0 text-alert" />
              <div className="min-w-0 flex-1">
                <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-alert">
                  {lastError.code.replace(/_/g, ' ')}
                </p>
                <p className="mt-0.5 text-xs leading-snug text-slate-300">{lastError.message}</p>
              </div>
              <button
                type="button"
                onClick={dismissError}
                className="shrink-0 rounded p-1 text-slate-500 transition-colors hover:text-slate-200"
                aria-label="Dismiss"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
