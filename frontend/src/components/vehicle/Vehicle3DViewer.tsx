import { Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { Edges, Html, OrbitControls } from '@react-three/drei'
import * as THREE from 'three'
import type { OrbitControls as OrbitControlsImpl } from 'three-stdlib'
import { Crosshair, Maximize2 } from 'lucide-react'
import { cn } from '@/lib/cn'
import { HEALTH_COLOR } from '@/lib/format'
import type { ModuleHealth, ModuleState } from '@/types/protocol'
import {
  buildBodyGeometry,
  MODULE_ANCHORS,
  WHEELS,
  WHEEL_RADIUS,
  WHEEL_SENSORS,
  WHEEL_WIDTH,
  type FocusPoint,
} from './geometry'

const NEON = '#38bdf8'
const HOME_TARGET = new THREE.Vector3(0, 0.82, 0)
const HOME_CAMERA = new THREE.Vector3(4.1, 2.8, 4.6)

/** How far the camera sits from a focused component. */
const FOCUS_DISTANCE = 2.6

interface Vehicle3DViewerProps {
  modules: ModuleState[]
  /** Where to fly the camera, or null to return to the overview. */
  focus: FocusPoint | null
  onSelectModule: (moduleName: string) => void
  className?: string
}

function healthOf(modules: ModuleState[], name: string): ModuleHealth {
  return modules.find((module) => module.name === name)?.health ?? 'unknown'
}

/**
 * Camera choreography.
 *
 * Lerped rather than cut: a jump loses the viewer's sense of where the part is
 * relative to the car, which is the one thing this view exists to convey. The
 * approach vector is biased outward and upward so the camera ends up looking
 * *at* the component from outside the bodywork instead of inside it.
 */
function CameraDirector({ focus }: { focus: FocusPoint | null }) {
  const controls = useRef<OrbitControlsImpl | null>(null)
  const { camera } = useThree()
  const targetGoal = useRef(HOME_TARGET.clone())
  const cameraGoal = useRef(HOME_CAMERA.clone())

  useEffect(() => {
    if (!focus) {
      targetGoal.current.copy(HOME_TARGET)
      cameraGoal.current.copy(HOME_CAMERA)
      return
    }
    const point = new THREE.Vector3(...focus.position)
    targetGoal.current.copy(point)

    // Approach from the nearest outside corner: push out along the component's
    // own lateral sign, and always from above, so bodywork never occludes it.
    const lateral = point.z >= 0 ? 1 : -1
    const longitudinal = point.x >= 0 ? 1 : -1
    cameraGoal.current.set(
      point.x + longitudinal * FOCUS_DISTANCE * 0.75,
      point.y + FOCUS_DISTANCE * 0.72,
      point.z + lateral * FOCUS_DISTANCE,
    )
  }, [focus])

  useFrame((_state, delta) => {
    // Frame-rate independent smoothing: a fixed lerp factor animates at a
    // different speed on a 144 Hz screen than on a 60 Hz one.
    const alpha = 1 - Math.pow(0.0016, delta)
    camera.position.lerp(cameraGoal.current, alpha)
    const control = controls.current
    if (control) {
      control.target.lerp(targetGoal.current, alpha)
      control.update()
    }
  })

  return (
    <OrbitControls
      ref={controls}
      enablePan={false}
      enableDamping
      dampingFactor={0.08}
      minDistance={2}
      maxDistance={14}
      // Stop below the horizon: underneath the car there is nothing to see and
      // the model reads as broken.
      maxPolarAngle={Math.PI * 0.49}
      minPolarAngle={Math.PI * 0.08}
      autoRotate={!focus}
      autoRotateSpeed={0.45}
    />
  )
}

/** The body: translucent matte shell plus a cyan wireframe over its edges. */
function Chassis() {
  const geometry = useMemo(() => buildBodyGeometry(), [])
  useEffect(() => () => geometry.dispose(), [geometry])

  return (
    <mesh geometry={geometry} castShadow={false}>
      {/* Near-black shell so the cyan edges carry the form. A lighter fill
          turns the model into a grey mass and the wireframe stops reading. */}
      <meshStandardMaterial
        color="#04121f"
        transparent
        opacity={0.42}
        roughness={0.85}
        metalness={0.05}
        emissive="#0a2a3f"
        emissiveIntensity={0.35}
        flatShading
      />
      <Edges threshold={18} color={NEON} />
    </mesh>
  )
}

function Wheels() {
  return (
    <>
      {WHEELS.map((wheel) => (
        <mesh
          key={wheel.label}
          position={wheel.position}
          rotation={[Math.PI / 2, 0, 0]}
        >
          <cylinderGeometry args={[WHEEL_RADIUS, WHEEL_RADIUS, WHEEL_WIDTH, 22]} />
          <meshStandardMaterial color="#03101c" transparent opacity={0.62} roughness={0.95} />
          <Edges threshold={28} color="#2b7fa8" />
        </mesh>
      ))}
    </>
  )
}

interface PinProps {
  position: [number, number, number]
  label: string
  health: ModuleHealth
  selected: boolean
  onSelect: () => void
}

/**
 * A module pin: a glowing sphere with expanding radar rings.
 *
 * Only faulted and selected pins ping. Everything pulsing at once is just
 * noise, and the ping is meant to draw the eye to the thing that is wrong.
 */
function ModulePin({ position, label, health, selected, onSelect }: PinProps) {
  const ringOne = useRef<THREE.Mesh>(null)
  const ringTwo = useRef<THREE.Mesh>(null)
  const core = useRef<THREE.Mesh>(null)
  const [hovered, setHovered] = useState(false)

  const color = HEALTH_COLOR[health]
  const active = health === 'fault' || selected

  useFrame((state) => {
    const time = state.clock.getElapsedTime()

    if (core.current) {
      const scale = active ? 1 + Math.sin(time * 3.4) * 0.16 : 1
      core.current.scale.setScalar(scale * (hovered ? 1.35 : 1))
    }

    // Two rings, half a period apart, so the ping is continuous.
    for (const [index, ring] of [ringOne, ringTwo].entries()) {
      const mesh = ring.current
      if (!mesh) continue
      if (!active) {
        mesh.visible = false
        continue
      }
      mesh.visible = true
      const phase = (time * 0.6 + index * 0.5) % 1
      mesh.scale.setScalar(0.6 + phase * 3.4)
      const material = mesh.material as THREE.MeshBasicMaterial
      material.opacity = (1 - phase) * 0.5
    }
  })

  return (
    <group position={position}>
      <mesh
        ref={core}
        onPointerOver={(event) => {
          event.stopPropagation()
          setHovered(true)
          document.body.style.cursor = 'pointer'
        }}
        onPointerOut={() => {
          setHovered(false)
          document.body.style.cursor = 'auto'
        }}
        onClick={(event) => {
          event.stopPropagation()
          onSelect()
        }}
      >
        <sphereGeometry args={[0.075, 18, 18]} />
        <meshBasicMaterial color={color} toneMapped={false} />
      </mesh>

      {/* Halo */}
      <mesh scale={selected ? 2.1 : 1.6}>
        <sphereGeometry args={[0.075, 14, 14]} />
        <meshBasicMaterial color={color} transparent opacity={0.16} toneMapped={false} />
      </mesh>

      {/* Radar rings, lying flat so they read as ground-plane pings. */}
      {[ringOne, ringTwo].map((ref, index) => (
        <mesh key={index} ref={ref} rotation={[-Math.PI / 2, 0, 0]}>
          <ringGeometry args={[0.07, 0.085, 32]} />
          <meshBasicMaterial
            color={color}
            transparent
            opacity={0}
            side={THREE.DoubleSide}
            toneMapped={false}
          />
        </mesh>
      ))}

      {(selected || hovered) && (
        <Html
          center
          distanceFactor={7}
          position={[0, 0.28, 0]}
          zIndexRange={[20, 0]}
          style={{ pointerEvents: 'none' }}
        >
          <div
            className="whitespace-nowrap rounded-md border px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.16em] backdrop-blur-sm"
            style={{
              borderColor: `${color}66`,
              color,
              background: 'rgba(11,15,25,0.86)',
              boxShadow: `0 0 16px ${color}44`,
            }}
          >
            {label}
          </div>
        </Html>
      )}
    </group>
  )
}

/** A faint reference plane so the car does not float in a void. */
function GroundPlane() {
  return (
    // A grid alone reads as a floor. The filled disc that used to sit under it
    // composited to a pale wash over the dark page and swallowed the model.
    <gridHelper
      args={[11, 22, new THREE.Color('#215a7d'), new THREE.Color('#102636')]}
      position={[0, 0.002, 0]}
    />
  )
}

function Scene({
  modules,
  focus,
  onSelectModule,
  selected,
}: Vehicle3DViewerProps & { selected: string | null }) {
  return (
    <>
      <ambientLight intensity={0.35} />
      <directionalLight position={[5, 7, 4]} intensity={0.5} color="#7fd3ff" />
      <directionalLight position={[-5, 3, -5]} intensity={0.28} color="#38bdf8" />
      <GroundPlane />
      <Chassis />
      <Wheels />

      {MODULE_ANCHORS.map((anchor) => (
        <ModulePin
          key={anchor.module}
          position={anchor.position}
          label={anchor.label}
          health={healthOf(modules, anchor.module)}
          selected={selected === anchor.label || focus?.label === anchor.label}
          onSelect={() => onSelectModule(anchor.module)}
        />
      ))}

      {WHEEL_SENSORS.map((sensor) => (
        <ModulePin
          key={sensor.id}
          position={sensor.position}
          label={sensor.label}
          // The corner sensors inherit the ABS module's health: they are the
          // parts it reports on.
          health={healthOf(modules, 'ABS')}
          selected={focus?.label === sensor.label}
          onSelect={() => onSelectModule('ABS')}
        />
      ))}

      <CameraDirector focus={focus} />
    </>
  )
}

function CanvasFallback({ message }: { message: string }) {
  return (
    <div className="flex h-full w-full items-center justify-center px-6 text-center">
      <div>
        <div className="mx-auto mb-3 h-8 w-8 animate-breathe rounded-full border-2 border-neon/40" />
        <p className="text-xs uppercase tracking-[0.2em] text-slate-500">{message}</p>
      </div>
    </div>
  )
}

/**
 * The interactive vehicle view.
 *
 * WebGL is not guaranteed — a locked-down workshop tablet, a remote session,
 * a browser with hardware acceleration off. Rather than showing an empty box,
 * the component detects that and says so, and the rest of the HUD carries on
 * working without it.
 */
export function Vehicle3DViewer({
  modules,
  focus,
  onSelectModule,
  className,
}: Vehicle3DViewerProps) {
  const [selected, setSelected] = useState<string | null>(null)
  const [webglOk] = useState(() => detectWebGL())

  useEffect(() => {
    if (focus?.label) setSelected(focus.label)
  }, [focus])

  return (
    <div className={cn('relative h-full w-full overflow-hidden rounded-2xl', className)}>
      {webglOk ? (
        <Canvas
          camera={{ position: HOME_CAMERA.toArray(), fov: 42, near: 0.1, far: 100 }}
          dpr={[1, 1.8]}
          gl={{ antialias: true, alpha: true, powerPreference: 'high-performance' }}
          style={{ background: 'transparent' }}
        >
          <Suspense fallback={null}>
            <Scene
              modules={modules}
              focus={focus}
              onSelectModule={(name) => {
                setSelected(name)
                onSelectModule(name)
              }}
              selected={selected}
            />
          </Suspense>
        </Canvas>
      ) : (
        <CanvasFallback message="3D view unavailable — WebGL is disabled in this browser" />
      )}

      {/* Overlay chrome */}
      <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between p-3">
        <div className="flex items-center gap-2 rounded-lg border border-white/10 bg-black/40 px-2.5 py-1.5 backdrop-blur-sm">
          <Crosshair className="h-3.5 w-3.5 text-neon" />
          <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-300">
            {focus ? focus.label : 'Chassis overview'}
          </span>
        </div>
        {focus && (
          <div className="rounded-lg border border-neon/25 bg-black/45 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.14em] text-neon backdrop-blur-sm">
            {focus.where}
          </div>
        )}
      </div>

      <div className="pointer-events-none absolute inset-x-0 bottom-0 flex items-center justify-between p-3">
        <span className="text-[10px] uppercase tracking-[0.16em] text-slate-600">
          Freelander 2 · L359
        </span>
        <span className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.16em] text-slate-600">
          <Maximize2 className="h-3 w-3" />
          drag to orbit · scroll to zoom
        </span>
      </div>
    </div>
  )
}

function detectWebGL(): boolean {
  try {
    const canvas = document.createElement('canvas')
    return Boolean(
      canvas.getContext('webgl2') ??
        canvas.getContext('webgl') ??
        canvas.getContext('experimental-webgl'),
    )
  } catch {
    return false
  }
}
