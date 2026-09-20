/**
 * 视觉核对专用构建配置（由 tools/visual-check.mjs 调用，一般不单独执行）。
 * 只打包 visual-check.html 这一个入口，产物落到 VISUAL_CHECK_OUT 指定的
 * 系统 Temp 目录，避开沙箱里 vite 清空 dist 触发的 safe-delete 拦截。
 */
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: process.env.VISUAL_CHECK_OUT || 'visual-check-dist',
    emptyOutDir: false,
    rollupOptions: {
      input: fileURLToPath(new URL('./visual-check.html', import.meta.url)),
    },
  },
});
