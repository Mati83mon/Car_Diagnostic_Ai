import * as THREE from 'three'

import { CAR_WIDTH } from './geometry'

/**
 * Photogrammetry chassis: loading, alignment and the holographic material.
 *
 * The mesh under `/models/freelander2.glb` was reconstructed from photographs
 * of a Freelander 2 and simplified to ~35k triangles. It arrives in the
 * conventions of the tool that produced it rather than ours:
 *
 *   - its length runs along +z, with the *front* of the car at +z;
 *   - it is normalised into roughly a unit box, so a metre means nothing;
 *   - it is centred on its own bounding box, so it floats half-buried;
 *   - it carries POSITION only. No normals, no UVs, no materials.
 *
 * Everything below exists to turn that into the scene's convention: length
 * along x with the front at -x, height in y measured up from the ground plane,
 * width in z straddling the axis. Getting this wrong is not a cosmetic problem
 * -- the module pins are authored at surveyed metre coordinates, so a car that
 * is flipped or mis-scaled puts the ECM pin in the boot.
 */

/** Served from `frontend/public`, so it is fetched rather than bundled. */
export const SCAN_MODEL_URL = '/models/freelander2.glb'

/**
 * The box the scan is fitted into, in scene metres.
 *
 * Length and roofline are taken from the procedural body in `geometry.ts` so
 * the two representations agree: the pins were placed against that body, and
 * they must keep landing on the same sheet metal once the scan replaces it.
 * The scan additionally includes wheels, so it is sat on the ground plane
 * (`minY` 0) rather than on the procedural body's sill.
 */
export const SCAN_TARGET = {
  /** Front bumper, matching the procedural profile's leading edge. */
  minX: -2.26,
  /** Rear bumper. */
  maxX: 2.2,
  /** Tyre contact patch. */
  minY: 0,
  /** Roofline, including the roof rails. */
  maxY: 1.87,
  /**
   * Widest point: mirror tip to mirror tip. The scan includes door mirrors,
   * so fitting its full width to the 1.91 m body width would pinch the
   * bodywork in by the width of both mirrors.
   */
  width: CAR_WIDTH + 0.25,
} as const

/**
 * Quarter turn that maps the scan's forward axis onto the scene's.
 *
 * The scan faces +z; the scene puts the front at -x. Rotating by -90 degrees
 * about y sends (0,0,1) to (-1,0,0), which is exactly that.
 */
const FORWARD_CORRECTION = -Math.PI / 2

/** Below this a bounding-box extent is treated as degenerate. */
const MIN_EXTENT = 1e-6

/**
 * Fit a raw scanned geometry into {@link SCAN_TARGET}.
 *
 * Returns a new geometry; the source is left untouched so a cached loader
 * result stays reusable. Scaling is per-axis on purpose. The reconstruction
 * came out about a tenth short in length for its width, and a uniform scale
 * would force a choice between a car of the right length that is too wide and
 * a car of the right width that is too short. Neither keeps the pins honest,
 * and at the sizes involved the stretch is not visible.
 */
export function fitScannedChassis(source: THREE.BufferGeometry): THREE.BufferGeometry {
  const geometry = source.clone()
  geometry.rotateY(FORWARD_CORRECTION)

  geometry.computeBoundingBox()
  const raw = geometry.boundingBox
  if (!raw) return finish(geometry)

  const size = raw.getSize(new THREE.Vector3())
  if (size.x < MIN_EXTENT || size.y < MIN_EXTENT || size.z < MIN_EXTENT) {
    // A degenerate box means the asset is not what we think it is. Leave the
    // geometry alone rather than scaling by infinity and blanking the view.
    return finish(geometry)
  }

  geometry.scale(
    (SCAN_TARGET.maxX - SCAN_TARGET.minX) / size.x,
    (SCAN_TARGET.maxY - SCAN_TARGET.minY) / size.y,
    SCAN_TARGET.width / size.z,
  )

  geometry.computeBoundingBox()
  const scaled = geometry.boundingBox
  if (scaled) {
    geometry.translate(
      SCAN_TARGET.minX - scaled.min.x,
      SCAN_TARGET.minY - scaled.min.y,
      -(scaled.min.z + scaled.max.z) / 2,
    )
  }

  return finish(geometry)
}

function finish(geometry: THREE.BufferGeometry): THREE.BufferGeometry {
  // The asset ships without normals to keep it small. They are needed for the
  // fresnel term, and computing them for 35k triangles costs a few
  // milliseconds once at load.
  geometry.computeVertexNormals()
  geometry.computeBoundingBox()
  geometry.computeBoundingSphere()
  return geometry
}

/**
 * Collapse a loaded glTF scene into one geometry in scene space.
 *
 * glTF nodes carry their own transforms, and the quantised asset leans on that
 * (`KHR_mesh_quantization` stores integer positions plus a node scale), so the
 * world matrix has to be baked in rather than ignored.
 */
export function extractGeometry(root: THREE.Object3D): THREE.BufferGeometry | null {
  const positions: number[] = []
  const indices: number[] = []
  const vertex = new THREE.Vector3()
  let vertexOffset = 0
  let found = false

  root.updateWorldMatrix(true, true)

  root.traverse((child) => {
    const mesh = child as THREE.Mesh
    if (!mesh.isMesh || !mesh.geometry) return
    const attribute = mesh.geometry.getAttribute('position')
    if (!attribute) return
    found = true

    // Read through Vector3 rather than transforming the attribute in place.
    // The asset is quantised (`KHR_mesh_quantization`), so its positions are
    // normalised 16-bit integers and the real scale lives in the node matrix.
    // `applyMatrix4` on such an attribute writes metres back into normalised
    // integer storage, where everything saturates at +-1 -- which renders as a
    // tidy unit cube instead of a car. `fromBufferAttribute` de-quantises to
    // float first, so the transform lands on real numbers.
    for (let i = 0; i < attribute.count; i += 1) {
      vertex.fromBufferAttribute(attribute, i).applyMatrix4(mesh.matrixWorld)
      positions.push(vertex.x, vertex.y, vertex.z)
    }

    // Indices are preserved rather than expanded. Shared vertices are what
    // make `computeVertexNormals` produce smooth normals, and the fresnel term
    // needs those: per-face normals turn the body into visible facets.
    const index = mesh.geometry.getIndex()
    if (index) {
      for (let i = 0; i < index.count; i += 1) indices.push(index.getX(i) + vertexOffset)
    } else {
      for (let i = 0; i < attribute.count; i += 1) indices.push(i + vertexOffset)
    }
    vertexOffset += attribute.count
  })

  if (!found) return null

  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3))
  geometry.setIndex(indices)
  return geometry
}

/**
 * Rim-lit hologram.
 *
 * A plain wireframe was the obvious first choice and it does not survive
 * contact with a scanned mesh: 35k triangles of edges reads as a solid cyan
 * blob, not a car. What carries the form instead is a fresnel term -- surfaces
 * facing away from the viewer glow, surfaces facing it stay dark -- with a
 * separate pass of *significant* edges over the top. Additive blending and no
 * depth write let the far side of the car show through, which is what makes it
 * look projected rather than painted.
 */
export function createHologramMaterial(color: THREE.ColorRepresentation): THREE.ShaderMaterial {
  return new THREE.ShaderMaterial({
    uniforms: {
      uColor: { value: new THREE.Color(color) },
      uOpacity: { value: 1 },
    },
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
    blending: THREE.AdditiveBlending,
    vertexShader: /* glsl */ `
      varying vec3 vNormalView;
      varying vec3 vViewDir;
      void main() {
        vNormalView = normalize(normalMatrix * normal);
        vec4 viewPosition = modelViewMatrix * vec4(position, 1.0);
        vViewDir = normalize(-viewPosition.xyz);
        gl_Position = projectionMatrix * viewPosition;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform vec3 uColor;
      uniform float uOpacity;
      varying vec3 vNormalView;
      varying vec3 vViewDir;
      void main() {
        float facing = abs(dot(normalize(vNormalView), normalize(vViewDir)));
        float rim = pow(1.0 - facing, 2.2);
        gl_FragColor = vec4(uColor * (0.07 + rim * 1.7), (0.10 + rim * 0.75) * uOpacity);
      }
    `,
  })
}

/**
 * Angle above which a shared edge is drawn.
 *
 * Tuned by eye against the real asset. At 12 degrees every triangle boundary
 * survives and the car fills in solid; at 40 the panel gaps, arches, glass
 * line and grille remain and little else, which is about 5k segments.
 */
export const EDGE_THRESHOLD_DEGREES = 40
