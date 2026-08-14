import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 本仓库 vite config 未装 @types/node（sandbox 下也无法 npm i）。
// Vite 用 esbuild 跑此文件，运行时不依赖这些类型；下方用最小声明垫平 tsc。
// 等统一补 @types/node 后，删除 `declare const process` 与 `// @ts-ignore` 即可。
declare const process: { cwd(): string };

// @ts-ignore - node:fs 类型由 @types/node 提供，构建期 esbuild 直接解析
import * as fs from 'node:fs'

// 落盘目录：<前端 cwd 的上一级>/data/edit-traces（与 data/ 下其他运行时产物同层）
const TRACE_DIR = process.cwd() + '/../data/edit-traces'

// 仅开发期：把前端采集到的编辑轨迹 append 到本地文件，不碰 Python 后端处理
function editTraceSink() {
  return {
    name: 'edit-trace-sink',
    configureServer(server: { middlewares: { use: (p: string, fn: (req: any, res: any) => void) => void } }) {
      server.middlewares.use('/__edit_trace', (req, res) => {
        if (req.method !== 'POST') {
          res.statusCode = 405
          res.end()
          return
        }
        let chunks = ''
        req.on('data', (c: string) => {
          chunks += c
          if (chunks.length > 5_000_000) req.destroy() // 单批上限 5MB，防失控
        })
        req.on('end', () => {
          try {
            const { sessionId, events } = JSON.parse(chunks) as {
              sessionId?: string
              events?: Array<Record<string, unknown>>
            }
            if (!sessionId || !Array.isArray(events) || events.length === 0) {
              res.statusCode = 400
              res.end('bad payload')
              return
            }
            fs.mkdirSync(TRACE_DIR, { recursive: true })
            const safeId = String(sessionId).replace(/[^a-zA-Z0-9_-]/g, '_')
            const fp = TRACE_DIR + '/' + safeId + '.jsonl'
            const lines =
              events
                .map((e) =>
                  JSON.stringify({
                    ts: e.ts,
                    type: e.type,
                    code: e.code,
                    cursor: e.cursor,
                    change: e.change,
                    idleMs: e.idleMs,
                  }),
                )
                .join('\n') + '\n'
            fs.appendFileSync(fp, lines)
            res.statusCode = 204
            res.end()
          } catch {
            res.statusCode = 500
            res.end('err')
          }
        })
      })
    },
  }
}

export default defineConfig({
  plugins: [react(), editTraceSink()],
  server: {
    port: 5173,
    proxy: {
      '/session': 'http://localhost:8765',
      '/health': 'http://localhost:8765',
    },
  },
})
