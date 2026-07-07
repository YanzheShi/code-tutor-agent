/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        ct: {
          bg: '#0f172a',       // slate-900
          panel: '#1e293b',    // slate-800
          border: '#334155',   // slate-700
          text: '#e2e8f0',     // slate-200
          muted: '#94a3b8',    // slate-400
          accent: '#38bdf8',   // sky-400
          success: '#4ade80',  // green-400
          error: '#f87171',    // red-400
          warn: '#fbbf24',     // amber-400
        },
      },
    },
  },
  plugins: [],
}