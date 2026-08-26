import { useEffect, useRef, useState } from 'react'

/** Characters revealed per second. Fast enough to read along with. */
const CHARS_PER_SECOND = 420

function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true
  )
}

/**
 * Progressively reveal text.
 *
 * Driven by `requestAnimationFrame` against elapsed time rather than a
 * per-character interval, so the reveal runs at the same speed regardless of
 * refresh rate and does not queue hundreds of timers.
 *
 * Returns the full text immediately when the reader has asked for reduced
 * motion, or when `enabled` is false (replayed history should not re-type).
 */
export function useTypewriter(text: string, enabled = true): string {
  const [shown, setShown] = useState(enabled ? '' : text)
  const frameRef = useRef<number | null>(null)

  useEffect(() => {
    if (!enabled || prefersReducedMotion()) {
      setShown(text)
      return
    }

    let start: number | null = null
    setShown('')

    const step = (timestamp: number) => {
      if (start === null) start = timestamp
      const elapsed = (timestamp - start) / 1000
      const count = Math.floor(elapsed * CHARS_PER_SECOND)
      if (count >= text.length) {
        setShown(text)
        return
      }
      setShown(text.slice(0, count))
      frameRef.current = requestAnimationFrame(step)
    }

    frameRef.current = requestAnimationFrame(step)
    return () => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current)
    }
  }, [text, enabled])

  return shown
}
