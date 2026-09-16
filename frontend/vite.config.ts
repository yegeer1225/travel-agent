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
    },
  },
})
