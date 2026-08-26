/**
 * The page's background layer: grid mesh, vignette and a slow horizon sweep.
 *
 * Purely decorative and `aria-hidden`, sitting behind everything at z-0.
 */
export function GridBackdrop() {
  return (
    <div aria-hidden className="pointer-events-none fixed inset-0 z-0 overflow-hidden">
      <div className="grid-mesh absolute inset-0 opacity-40" />
      {/* Vignette: pulls attention to the centre columns. */}
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_center,transparent_0%,rgba(3,7,18,0.55)_100%)]" />
      {/* A single slow highlight passing across the top edge. One moving
          element reads as "live"; several read as noise. */}
      <div className="absolute inset-x-0 top-0 h-px overflow-hidden">
        <div className="h-px w-1/3 animate-sweep bg-gradient-to-r from-transparent via-neon/70 to-transparent" />
      </div>
    </div>
  )
}
