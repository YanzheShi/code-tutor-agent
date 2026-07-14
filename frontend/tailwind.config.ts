/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        ct: {
          bg: 'var(--ct-bg)',
          panel: 'var(--ct-panel)',
          border: 'var(--ct-border)',
          text: 'var(--ct-text)',
          muted: 'var(--ct-muted)',
          accent: 'var(--ct-accent)',
          success: 'var(--ct-success)',
          error: 'var(--ct-error)',
          warn: 'var(--ct-warn)',
          info: 'var(--ct-info)',
          surface: 'var(--ct-surface)',
          'surface-secondary': 'var(--ct-surface-secondary)',
          input: 'var(--ct-input)',
          hover: 'var(--ct-hover)',
          overlay: 'var(--ct-overlay)',
          'success-bg': 'var(--ct-success-bg)',
          'error-bg': 'var(--ct-error-bg)',
          'warn-bg': 'var(--ct-warn-bg)',
          'info-bg': 'var(--ct-info-bg)',
        },
      },
    },
  },
  plugins: [],
}