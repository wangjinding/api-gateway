import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// base 用 /web/：构建产物由网关挂载在同一源 /web 下，与 /v1 /auth /health 同源。
export default defineConfig({
  plugins: [vue()],
  base: '/web/',
  server: {
    port: 5173,
    proxy: {
      // 本地开发时把网关候选透到 8808
      '/v1': 'http://127.0.0.1:8808',
      '/auth': 'http://127.0.0.1:8808',
      '/health': 'http://127.0.0.1:8808',
      '/healthz': 'http://127.0.0.1:8808',
      '/metrics': 'http://127.0.0.1:8808',
      '/logs': 'http://127.0.0.1:8808',
      '/models': 'http://127.0.0.1:8808',
      // 第十八轮补: 此前缺 /history —— 本地开发时历史接口落到 vite 自己身上 404，
      //   页面弹「连不上服务端历史接口，暂时只用本机缓存显示」，看着像后端坏了。
      '/history': 'http://127.0.0.1:8808',
      // 第二十二轮: 意见反馈四个接口 —— 同样别让本地开发时落到 vite 自己身上
      //   （表现为页面报「Unexpected token '<'」，看着像后端崩了，实则是拿到了 index.html）。
      '/feedback': 'http://127.0.0.1:8808',
    },
  },
  build: {
    // M5: 剩余超 500kB 的都是独立 vendor chunk（element-plus / echarts），缓存友好，
    //    gzip 后约 340kB / 202kB。根因是 element-plus 全量引入，按需引入留作后续。
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        // M5: 拆 vendor chunk，避免 element-plus/echarts/marked 打进单个入口超大 chunk。
        manualChunks: {
          'vendor-vue': ['vue', 'vue-router', 'pinia'],
          'vendor-element': ['element-plus', '@element-plus/icons-vue'],
          'vendor-echarts': ['echarts'],
          'vendor-markdown': ['marked', 'marked-highlight', 'dompurify', 'highlight.js'],
        },
      },
    },
  },
})