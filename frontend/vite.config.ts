import { fileURLToPath, URL } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

/**
 * Конфигурация сборки SPA (§14.1 п. 5).
 *
 * `npm run build` кладёт результат в `frontend/dist` — именно эту папку отдаёт
 * сервер дашборда (`server/api/spa.py`), отдельный веб-сервер не нужен.
 */
export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // В режиме разработки API проксируется на локальный сервер дашборда
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['tests/**/*.spec.ts'],
    setupFiles: ['tests/setup.ts'],
    restoreMocks: true,
    clearMocks: true,
  },
})
