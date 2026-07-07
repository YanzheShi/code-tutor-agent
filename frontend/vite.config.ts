import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/session': 'http://localhost:8765',
      '/health': 'http://localhost:8765',
    },
  },
})