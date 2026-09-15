import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  plugins: [vue()],
  base: './',
  build: { outDir: '/tmp/gw-visual-dist', emptyOutDir: true, chunkSizeWarningLimit: 3000 },
})
