/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: { abyss: '#07110e', panel: '#0d1b16', line: '#1d4034', neon: '#53e89b' },
      boxShadow: { glow: '0 0 35px rgba(83,232,155,.10)' },
    },
  },
  plugins: [],
}
