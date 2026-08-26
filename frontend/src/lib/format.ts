import type { ModuleHealth, SignalReading } from '@/types/protocol'

/** Signal name -> the label shown on a gauge or readout. */
export const SIGNAL_LABELS: Record<string, string> = {
  RPM: 'Engine speed',
  THROTTLE_POS: 'Throttle',
  MODULE_VOLTAGE: 'Battery',
  COOLANT_TEMP: 'Coolant',
  MAP: 'Manifold',
  BAROMETRIC_PRESSURE: 'Barometric',
  MAF: 'Air flow',
  ENGINE_LOAD: 'Load',
  SPEED: 'Speed',
  INTAKE_TEMP: 'Intake air',
  FUEL_RAIL_PRESSURE: 'Rail pressure',
  OIL_TEMP: 'Oil',
}

/** Display range per signal, so a gauge sweep means something physical. */
export const SIGNAL_RANGES: Record<string, { min: number; max: number }> = {
  RPM: { min: 0, max: 5000 },
  THROTTLE_POS: { min: 0, max: 100 },
  MODULE_VOLTAGE: { min: 8, max: 16 },
  COOLANT_TEMP: { min: -40, max: 130 },
  MAP: { min: 0, max: 255 },
  BAROMETRIC_PRESSURE: { min: 0, max: 130 },
  MAF: { min: 0, max: 60 },
  ENGINE_LOAD: { min: 0, max: 100 },
  SPEED: { min: 0, max: 200 },
  INTAKE_TEMP: { min: -40, max: 90 },
  FUEL_RAIL_PRESSURE: { min: 0, max: 180000 },
  OIL_TEMP: { min: -40, max: 150 },
}

export function labelFor(signal: string): string {
  return SIGNAL_LABELS[signal] ?? signal.replace(/_/g, ' ').toLowerCase()
}

export function rangeFor(signal: string): { min: number; max: number } {
  return SIGNAL_RANGES[signal] ?? { min: 0, max: 100 }
}

/** Numeric value, or null when the reading is not a number. */
export function numeric(reading: SignalReading | undefined): number | null {
  if (!reading) return null
  const { value } = reading
  if (typeof value === 'number') return value
  if (typeof value === 'string') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

/** Compact display string: big numbers lose their decimals, small keep one. */
export function formatValue(value: number | null, unit: string): string {
  if (value === null) return '—'
  const abs = Math.abs(value)
  if (abs >= 1000) return `${Math.round(value).toLocaleString()}${unit ? ` ${unit}` : ''}`
  if (abs >= 100) return `${Math.round(value)}${unit ? ` ${unit}` : ''}`
  return `${value.toFixed(1)}${unit ? ` ${unit}` : ''}`
}

export function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value))
}

/** Where a value sits in its range, 0..1. */
export function fraction(signal: string, value: number | null): number {
  if (value === null) return 0
  const { min, max } = rangeFor(signal)
  if (max === min) return 0
  return clamp01((value - min) / (max - min))
}

export const HEALTH_COLOR: Record<ModuleHealth, string> = {
  online: '#10b981',
  fault: '#f43f5e',
  offline: '#64748b',
  unknown: '#475569',
}

export const HEALTH_LABEL: Record<ModuleHealth, string> = {
  online: 'ONLINE',
  fault: 'DTC FOUND',
  offline: 'NO RESPONSE',
  unknown: 'NOT QUERIED',
}

export function timeOfDay(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}
