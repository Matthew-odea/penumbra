/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        surface: {
          0: '#0a0a0a',
          1: '#111111',
          2: '#191919',
          3: '#222222',
        },
        border: {
          subtle: '#1e1e1e',
          DEFAULT: '#2a2a2a',
          emphasis: '#3a3a3a',
        },
        accent: {
          DEFAULT: '#f59e0b',
          dim: '#b45309',
          bright: '#fbbf24',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['JetBrains Mono', 'SF Mono', 'Menlo', 'monospace'],
      },
      borderRadius: {
        sm: '3px',
        DEFAULT: '4px',
        md: '6px',
      },
    },
  },
  plugins: [],
}
