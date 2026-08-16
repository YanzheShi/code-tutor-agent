import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 编辑轨迹采集（useEditTrace）现改为实时上报到后端接收 API
//   POST /session/{id}/edit-trace
// 由下方 proxy 转发到后端 8765，不再经过前端 dev 中间件落盘 JSONL。
// 旧的 dev 中间件 edit-trace-sink（append 到 data/edit-traces/*.jsonl）已退役，
// 因其绕过后端、且生产环境无 vite dev server。详见 docs/error-mode-tracking-design.md。

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
