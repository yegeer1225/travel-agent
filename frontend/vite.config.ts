import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // 前端统一请求 /api/xxx，由 Vite 转发到后端
      // ⚠️ 前端不要写死后端端口，也不要写死 http://127.0.0.1:8000
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      // 头像等静态资源（后端返回相对路径 /uploads/...，img 加载须经 Vite 转发到后端，
      // 否则会落在 Vite 的 SPA 兜底上返回 index.html → 图片加载失败头像消失，2026-09-20）
      '/uploads': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
