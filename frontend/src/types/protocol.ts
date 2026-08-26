/**
 * The `/ws/diagnostics` message contract.
 *
 * Mirrors `majster_ai/web/protocol.py`. Keep the two in step: the server is
 * the source of truth, and every frame is a flat `{ type, ... }` union so a
 * `switch` narrows it cleanly.
 */

export type AgentState =
  | 'idle'
  | 'thinking'
  | 'tool'
  | 'awaiting_approval'
  | 'error'

/** A module's traffic-light state. `offline` is not the same as healthy. */
export type ModuleHealth = 'online' | 'fault' | 'offline' | 'unknown'

export type RiskLevel = 'low' | 'medium' | 'high'

export interface InterfaceInfo {
  backend: string
  channel: string
  bitrate: number
  /**
   * False for the built-in simulator. The UI must say so plainly — a mechanic
   * acting on synthetic readings believing they came from the car is the worst
   * outcome this interface can produce.
   */
  physical: boolean
  safety_mode: string
  write_enabled: boolean
  require_approval: boolean
}

export interface DtcStatus {
  raw: string
  flags: string[]
  confirmed: boolean
  pending: boolean
  active: boolean
  warning_indicator: boolean
  explanations: string[]
}

export interface Dtc {
  code: string
  full_code: string
  failure_type: string
  system: string
  generic: boolean
  description: string
  module: string | null
  status: DtcStatus
  raw: string
}

export interface ModuleState {
  name: string
  description: string
  address: string
  /** False for community-derived addresses: silence may just mean it's wrong. */
  verified: boolean
  health: ModuleHealth
  dtc_count: number
  dtcs: Dtc[]
  detail: string
}

export interface SignalReading {
  signal: string
  value: number | string | null
  unit: string
  description: string
  /** Set when the value is physically implausible — itself diagnostic evidence. */
  warning: string | null
  verified_scaling: boolean
}

export interface Citation {
  kind: 'manual' | 'web' | 'vehicle'
  label: string
  detail: string
  url: string | null
  score: number | null
}

interface BaseFrame {
  type: string
  ts: number
}

export interface HelloFrame extends BaseFrame {
  type: 'hello'
  project: string
  version: string
  vehicle: string
  interface: InterfaceInfo
  modules: ModuleState[]
  telemetry_signals: string[]
  telemetry_interval_ms: number
}

export interface ModulesFrame extends BaseFrame {
  type: 'modules'
  modules: ModuleState[]
  total_dtcs: number
}

export interface TelemetryFrame extends BaseFrame {
  type: 'telemetry'
  readings: SignalReading[]
  /** True when the poll failed and these are the previous values. */
  stale: boolean
}

export interface AgentStatusFrame extends BaseFrame {
  type: 'agent.status'
  state: AgentState
  detail: string
}

export interface AgentToolFrame extends BaseFrame {
  type: 'agent.tool'
  tool: string
  arguments: Record<string, unknown>
  ok: boolean
  summary: string
}

export interface AgentMessageFrame extends BaseFrame {
  type: 'agent.message'
  role: 'user' | 'assistant' | 'system'
  text: string
  citations: Citation[]
  tools_used: string[]
}

/**
 * A write is paused, waiting for a human.
 *
 * Note what is absent: the service's confirmation token. This frame carries an
 * opaque `approval_id` and nothing else that could authorise anything. The
 * browser answers the question; it cannot pose one, and it cannot mint the
 * credential that performs the write.
 */
export interface ApprovalRequestFrame extends BaseFrame {
  type: 'approval.request'
  approval_id: string
  tool: string
  module: string
  risk: RiskLevel
  scope: string
  reversible: boolean
  affected_codes: Dtc[]
  risks: string[]
  address_verified: boolean
  expires_in_seconds: number
}

export interface ApprovalResolvedFrame extends BaseFrame {
  type: 'approval.resolved'
  approval_id: string
  approved: boolean
  reason: string
}

export interface ErrorFrame extends BaseFrame {
  type: 'error'
  code: string
  message: string
}

export interface PongFrame extends BaseFrame {
  type: 'pong'
}

export type ServerFrame =
  | HelloFrame
  | ModulesFrame
  | TelemetryFrame
  | AgentStatusFrame
  | AgentToolFrame
  | AgentMessageFrame
  | ApprovalRequestFrame
  | ApprovalResolvedFrame
  | ErrorFrame
  | PongFrame

export type ClientCommand =
  | { type: 'chat'; text: string }
  | { type: 'approval.response'; approval_id: string; approved: boolean }
  | { type: 'refresh'; modules?: string[] }
  | { type: 'ping' }

/** A message in the terminal, including locally-generated system notices. */
export interface ChatEntry {
  id: string
  role: 'user' | 'assistant' | 'system'
  text: string
  citations: Citation[]
  toolsUsed: string[]
  ts: number
  /** Assistant text animates in; replayed history does not. */
  animate?: boolean
}

export interface ToolActivity {
  id: string
  tool: string
  ok: boolean
  summary: string
  ts: number
}
