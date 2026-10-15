import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

/**
 * 开发环境后端地址（spec/022 的基址）。
 * 只是默认值，可用环境变量覆盖；前端不保存任何密钥。
 */
const PROXY_TARGET = process.env.VITE_PROXY_TARGET ?? 'http://127.0.0.1:8012'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // 开发时把 /api 代理到后端：前端代码只写 /api/... ，无需为跨域做特殊配置
    proxy: {
      '/api': {
        target: PROXY_TARGET,
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.spec.ts'],
  },
})
