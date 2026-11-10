import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: '#0a0b0f',
        surface: '#12141a',
        graphite: '#1b1e26',
        border: '#262a33',
        tg: {
          blue: '#2AABEE',
          glow: '#229ED9',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
      },
      boxShadow: {
        glow: '0 0 40px -10px rgba(42, 171, 238, 0.35)',
      },
      backdropBlur: { xs: '2px' },
    },
  },
  plugins: [],
};

export default config;
