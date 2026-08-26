import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  ApprovalRequestFrame,
  ChatEntry,
  ClientCommand,
  InterfaceInfo,
  ModuleState,
  ServerFrame,
  SignalReading,
  ToolActivity,
  AgentState,
} from '@/types/protocol'

/** How many telemetry frames the live plot keeps. ~60s at 500ms. */
const HISTORY_LENGTH = 120

/** Reconnect backoff, in ms. A workshop Wi-Fi drop should recover by itself. */
const RECONNECT_STEPS = [500, 1000, 2000, 4000, 8000]

export interface TelemetryPoint {
  t: number
  [signal: string]: number
}

export interface DiagnosticsState {
  connected: boolean
  connecting: boolean
  /** Set when the socket is down, so the UI can stop pretending it is live. */
  connectionError: string | null
  interfaceInfo: InterfaceInfo | null
  vehicle: string
  version: string
  modules: ModuleState[]
  totalDtcs: number
  readings: Record<string, SignalReading>
  /** True when the last telemetry frame was a repeat of stale values. */
  telemetryStale: boolean
  history: TelemetryPoint[]
  chat: ChatEntry[]
  tools: ToolActivity[]
  agentState: AgentState
  agentDetail: string
  pendingApproval: ApprovalRequestFrame | null
  lastError: { code: string; message: string } | null
}

export interface DiagnosticsApi extends DiagnosticsState {
  sendChat: (text: string) => void
  respondApproval: (approvalId: string, approved: boolean) => void
  refresh: () => void
  dismissError: () => void
}

function socketUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws/diagnostics`
}

let entryId = 0
const nextId = (): string => `e${++entryId}`

/**
 * The single connection to the diagnostic backend.
 *
 * Owns the socket, reconnection, and every piece of live state the HUD paints.
 * Deliberately one hook rather than a store: there is exactly one vehicle and
 * one socket, and threading that through context adds indirection without
 * buying anything.
 */
export function useDiagnostics(): DiagnosticsApi {
  const [state, setState] = useState<DiagnosticsState>({
    connected: false,
    connecting: true,
    connectionError: null,
    interfaceInfo: null,
    vehicle: '',
    version: '',
    modules: [],
    totalDtcs: 0,
    readings: {},
    telemetryStale: false,
    history: [],
    chat: [],
    tools: [],
    agentState: 'idle',
    agentDetail: '',
    pendingApproval: null,
    lastError: null,
  })

  const socketRef = useRef<WebSocket | null>(null)
  const attemptRef = useRef(0)
  const closedRef = useRef(false)
  const timerRef = useRef<number | null>(null)

  const handleFrame = useCallback((frame: ServerFrame) => {
    setState((previous) => {
      switch (frame.type) {
        case 'hello':
          return {
            ...previous,
            interfaceInfo: frame.interface,
            vehicle: frame.vehicle,
            version: frame.version,
            modules: frame.modules,
            totalDtcs: frame.modules.reduce((sum, m) => sum + m.dtc_count, 0),
          }

        case 'modules':
          return { ...previous, modules: frame.modules, totalDtcs: frame.total_dtcs }

        case 'telemetry': {
          const readings: Record<string, SignalReading> = {}
          const point: TelemetryPoint = { t: frame.ts }
          for (const reading of frame.readings) {
            readings[reading.signal] = reading
            if (typeof reading.value === 'number') point[reading.signal] = reading.value
          }
          const history = [...previous.history, point].slice(-HISTORY_LENGTH)
          return { ...previous, readings, history, telemetryStale: frame.stale }
        }

        case 'agent.status':
          return { ...previous, agentState: frame.state, agentDetail: frame.detail }

        case 'agent.tool':
          return {
            ...previous,
            tools: [
              ...previous.tools,
              {
                id: nextId(),
                tool: frame.tool,
                ok: frame.ok,
                summary: frame.summary,
                ts: frame.ts,
              },
            ].slice(-40),
          }

        case 'agent.message':
          return {
            ...previous,
            chat: [
              ...previous.chat,
              {
                id: nextId(),
                role: frame.role,
                text: frame.text,
                citations: frame.citations,
                toolsUsed: frame.tools_used,
                ts: frame.ts,
                animate: frame.role === 'assistant',
              },
            ],
          }

        case 'approval.request':
          return { ...previous, pendingApproval: frame }

        case 'approval.resolved':
          return {
            ...previous,
            pendingApproval:
              previous.pendingApproval?.approval_id === frame.approval_id
                ? null
                : previous.pendingApproval,
          }

        case 'error':
          return {
            ...previous,
            lastError: { code: frame.code, message: frame.message },
          }

        case 'pong':
        default:
          return previous
      }
    })
  }, [])

  const connect = useCallback(() => {
    if (closedRef.current) return
    setState((previous) => ({ ...previous, connecting: true }))

    let socket: WebSocket
    try {
      socket = new WebSocket(socketUrl())
    } catch (error) {
      setState((previous) => ({
        ...previous,
        connecting: false,
        connectionError: String(error),
      }))
      return
    }
    socketRef.current = socket

    socket.onopen = () => {
      attemptRef.current = 0
      setState((previous) => ({
        ...previous,
        connected: true,
        connecting: false,
        connectionError: null,
      }))
    }

    socket.onmessage = (event) => {
      try {
        handleFrame(JSON.parse(event.data as string) as ServerFrame)
      } catch {
        // A frame we cannot parse is a bug on one side, not a reason to drop
        // the connection and lose the telemetry stream with it.
      }
    }

    socket.onclose = () => {
      socketRef.current = null
      if (closedRef.current) return
      const delay = RECONNECT_STEPS[Math.min(attemptRef.current, RECONNECT_STEPS.length - 1)]
      attemptRef.current += 1
      setState((previous) => ({
        ...previous,
        connected: false,
        connecting: true,
        // A dropped socket also drops any pending approval server-side, so
        // clear it here rather than leave a dead prompt on screen.
        pendingApproval: null,
        connectionError: `Connection lost. Reconnecting in ${Math.round(delay / 1000)}s…`,
      }))
      timerRef.current = window.setTimeout(connect, delay)
    }

    socket.onerror = () => {
      // onclose always follows; it owns the retry.
    }
  }, [handleFrame])

  useEffect(() => {
    closedRef.current = false
    connect()
    return () => {
      closedRef.current = true
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      socketRef.current?.close()
      socketRef.current = null
    }
  }, [connect])

  const send = useCallback((command: ClientCommand) => {
    const socket = socketRef.current
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setState((previous) => ({
        ...previous,
        lastError: {
          code: 'not_connected',
          message: 'Not connected to the vehicle interface.',
        },
      }))
      return
    }
    socket.send(JSON.stringify(command))
  }, [])

  const sendChat = useCallback(
    (text: string) => {
      const trimmed = text.trim()
      if (!trimmed) return
      send({ type: 'chat', text: trimmed })
    },
    [send],
  )

  const respondApproval = useCallback(
    (approvalId: string, approved: boolean) => {
      send({ type: 'approval.response', approval_id: approvalId, approved })
      // Clear optimistically: the operator has decided, and leaving the panel
      // up invites a second, confusing answer.
      setState((previous) => ({ ...previous, pendingApproval: null }))
    },
    [send],
  )

  const refresh = useCallback(() => send({ type: 'refresh' }), [send])

  const dismissError = useCallback(
    () => setState((previous) => ({ ...previous, lastError: null })),
    [],
  )

  return useMemo(
    () => ({ ...state, sendChat, respondApproval, refresh, dismissError }),
    [state, sendChat, respondApproval, refresh, dismissError],
  )
}
