import * as THREE from 'three'

/**
 * Vehicle geometry for the 3D viewer.
 *
 * Coordinates are in metres, matching the real car so the module pins sit
 * where the components actually are:
 *   x  length, front negative      (-2.25 .. +2.25)
 *   y  height, ground at 0         (0 .. ~1.75)
 *   z  width, driver side negative (-0.95 .. +0.95)
 *
 * Freelander 2 (L359): 4.50 m long, 1.91 m wide, 1.74 m tall, 2.66 m wheelbase.
 * The silhouette is an extruded side profile rather than stacked boxes --
 * stacked boxes read as a lorry, and the whole point is that a mechanic
 * recognises the car.
 */

export const CAR_LENGTH = 4.5
export const CAR_WIDTH = 1.91
export const WHEEL_RADIUS = 0.38
export const WHEEL_WIDTH = 0.26

/** Side profile, front (-x) to rear (+x), over the roof and back underneath. */
const PROFILE: [number, number][] = [
  // Short overhangs and an upright tail: the Freelander 2 is a boxy compact
  // SUV, and a long sloping rear reads as an estate car instead.
  [-2.2, 0.42],
  [-2.26, 0.78],
  [-2.14, 1.06],
  [-1.8, 1.2],
  [-1.14, 1.3],
  [-0.62, 1.76],
  [-0.16, 1.86],
  [1.24, 1.87],
  [1.78, 1.8],
  [1.98, 1.28],
  [2.14, 1.1],
  [2.2, 0.72],
  [2.16, 0.42],
]

export function buildBodyGeometry(): THREE.ExtrudeGeometry {
  const shape = new THREE.Shape()
  shape.moveTo(PROFILE[0][0], PROFILE[0][1])
  for (const [x, y] of PROFILE.slice(1)) shape.lineTo(x, y)
  shape.closePath()

  const geometry = new THREE.ExtrudeGeometry(shape, {
    depth: CAR_WIDTH,
    bevelEnabled: true,
    bevelThickness: 0.05,
    bevelSize: 0.05,
    bevelSegments: 2,
    steps: 1,
  })
  // Extrusion runs along +z from 0; recentre so the car straddles the axis.
  geometry.translate(0, 0, -CAR_WIDTH / 2)
  geometry.computeVertexNormals()
  return geometry
}

export interface WheelPlacement {
  position: [number, number, number]
  label: string
}

/** Wheelbase 2.66 m, centred on the body. */
export const WHEELS: WheelPlacement[] = [
  { position: [-1.33, WHEEL_RADIUS, -0.82], label: 'FL' },
  { position: [-1.33, WHEEL_RADIUS, 0.82], label: 'FR' },
  { position: [1.33, WHEEL_RADIUS, -0.82], label: 'RL' },
  { position: [1.33, WHEEL_RADIUS, 0.82], label: 'RR' },
]

export interface ModuleAnchor {
  /** Matches the backend module name, so health maps straight onto the pin. */
  module: string
  label: string
  position: [number, number, number]
  /** Shown when the pin is selected. */
  where: string
}

/**
 * Where each module physically lives.
 *
 * The ABS entry is the controller; the four wheel-speed sensors it depends on
 * are drawn separately at the arches, because "ABS fault" almost always means
 * "one of those four", and pointing at the hydraulic unit would send someone
 * to the wrong end of the car.
 */
export const MODULE_ANCHORS: ModuleAnchor[] = [
  { module: 'ECM', label: 'ECM', position: [-1.6, 1.02, 0], where: 'Engine bay, centre' },
  { module: 'TCM', label: 'TCM', position: [-0.72, 0.5, 0], where: 'Transmission tunnel' },
  { module: 'ABS', label: 'ABS', position: [-1.66, 0.86, -0.55], where: 'Engine bay, driver side' },
  { module: 'HALDEX', label: 'HALDEX', position: [1.42, 0.44, 0], where: 'Rear axle coupling' },
  { module: 'RCM', label: 'RCM', position: [-0.15, 0.5, 0], where: 'Centre console, floor' },
  { module: 'CJB', label: 'CJB', position: [-1.08, 1.12, -0.62], where: 'Driver footwell' },
  { module: 'IPC', label: 'IPC', position: [-0.9, 1.42, -0.42], where: 'Instrument cluster' },
  { module: 'TRM', label: 'TRM', position: [0.1, 0.96, 0.3], where: 'Centre console' },
  { module: 'PBM', label: 'PBM', position: [0.55, 0.42, -0.5], where: 'Under rear seat' },
  { module: 'HVAC', label: 'HVAC', position: [-1.0, 0.88, 0.35], where: 'Behind dashboard' },
  { module: 'PAM', label: 'PAM', position: [2.08, 0.7, 0], where: 'Rear bumper' },
]

/**
 * Wheel-speed sensors, drawn at the arches.
 *
 * `dtcCodes` lets a chassis code select the right corner: C0034 is the
 * front-right sensor, so clicking that code flies the camera to that wheel
 * rather than to the ABS module in the engine bay.
 */
export interface WheelSensorAnchor {
  id: string
  label: string
  position: [number, number, number]
  where: string
  dtcCodes: string[]
}

export const WHEEL_SENSORS: WheelSensorAnchor[] = [
  {
    id: 'WSS_FL',
    label: 'WSS FL',
    position: [-1.33, 0.42, -0.9],
    where: 'Front left wheel arch',
    dtcCodes: ['C0031', 'C0032', 'C0033'],
  },
  {
    id: 'WSS_FR',
    label: 'WSS FR',
    position: [-1.33, 0.42, 0.9],
    where: 'Front right wheel arch',
    dtcCodes: ['C0034', 'C0035', 'C0036'],
  },
  {
    id: 'WSS_RL',
    label: 'WSS RL',
    position: [1.33, 0.42, -0.9],
    where: 'Rear left wheel arch',
    dtcCodes: ['C0037', 'C0038', 'C0039'],
  },
  {
    id: 'WSS_RR',
    label: 'WSS RR',
    position: [1.33, 0.42, 0.9],
    where: 'Rear right wheel arch',
    dtcCodes: ['C003A', 'C003B', 'C003C'],
  },
]

export interface FocusPoint {
  position: [number, number, number]
  label: string
  where: string
}

/**
 * The 3D point a fault code should focus.
 *
 * Wheel-speed codes resolve to their specific corner; everything else falls
 * back to the reporting module's own anchor.
 */
export function focusPointFor(code: string, moduleName: string | null): FocusPoint | null {
  const normalised = code.toUpperCase().split('-')[0]

  const sensor = WHEEL_SENSORS.find((entry) => entry.dtcCodes.includes(normalised))
  if (sensor) {
    return { position: sensor.position, label: sensor.label, where: sensor.where }
  }

  if (moduleName) {
    const anchor = MODULE_ANCHORS.find((entry) => entry.module === moduleName)
    if (anchor) {
      return { position: anchor.position, label: anchor.label, where: anchor.where }
    }
  }
  return null
}
