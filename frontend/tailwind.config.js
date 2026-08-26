/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Palette taken from Majster-AI_UI_UX_Concept.pdf.
        void: '#0b0f19', // deepest layer, behind the 3D canvas
        base: '#0f172a', // "Deep Space Background"
        panel: '#1e293b', // "Glass Panel Fill"
        inset: '#131c2e', // recessed panels inside a glass card
        hairline: '#243449',
        neon: {
          DEFAULT: '#38bdf8', // "Neon Cyan Accent"
          dim: '#0ea5e9',
          bright: '#7dd3fc',
        },
        alert: {
          DEFAULT: '#f43f5e', // "Alert Red Accent"
          dim: '#be123c',
        },
        telemetry: {
          DEFAULT: '#10b981', // "Telemetry Green"
          dim: '#059669',
        },
        caution: '#f59e0b',
      },
      fontFamily: {
        // Deliberately system stacks: this runs in workshops with no signal,
        // and a webfont that fails to load is worse than one that was never
        // requested. The "cyber-deck" feel comes from weight, tracking and
        // colour instead.
        sans: ['ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'Helvetica Neue', 'Arial', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'DejaVu Sans Mono', 'monospace'],
      },
      boxShadow: {
        neon: '0 0 15px rgba(56,189,248,0.35)',
        'neon-lg': '0 0 30px rgba(56,189,248,0.28)',
        alert: '0 0 18px rgba(244,63,94,0.40)',
        telemetry: '0 0 18px rgba(16,185,129,0.35)',
        panel: '0 18px 48px -24px rgba(0,0,0,0.85)',
      },
      keyframes: {
        sonar: {
          '0%': { transform: 'scale(1)', opacity: '0.55' },
          '100%': { transform: 'scale(2.6)', opacity: '0' },
        },
        breathe: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.45' },
        },
        sweep: {
          '0%': { transform: 'translateX(-120%)' },
          '100%': { transform: 'translateX(220%)' },
        },
        rise: {
          from: { opacity: '0', transform: 'translateY(6px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        sonar: 'sonar 2.4s cubic-bezier(0,0.2,0.4,1) infinite',
        breathe: 'breathe 2.2s ease-in-out infinite',
        sweep: 'sweep 2.8s ease-in-out infinite',
        rise: 'rise 260ms ease-out both',
      },
    },
  },
  plugins: [require('daisyui')],
  daisyui: {
    themes: [
      {
        majster: {
          primary: '#38bdf8',
          'primary-content': '#04121f',
          secondary: '#10b981',
          accent: '#7dd3fc',
          neutral: '#1e293b',
          'base-100': '#0f172a',
          'base-200': '#131c2e',
          'base-300': '#1e293b',
          'base-content': '#e2e8f0',
          info: '#38bdf8',
          success: '#10b981',
          warning: '#f59e0b',
          error: '#f43f5e',
        },
      },
    ],
    logs: false,
  },
}
