import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// base 用 /web/：构建产物由网关挂载在同一源 /web 下，与 /v1 /auth /health 同源。
export default defineConfig({
  plugins: [
    vue(),
    // 第二十八轮：element-plus 改按需引入（此前全量 app.use(ElementPlus)，vendor-element
    //   一个 chunk 就 1089kB / gzip 341kB，是首屏体积大头）。
    //   Components：模板里用到的 <el-*> 自动注入该组件 + 对应样式（importStyle 默认 css）。
    //   AutoImport：只管命令式 ElMessage/ElMessageBox —— 但我们代码里显式 import 了它们，
    //   样式在 main.ts 手动引了三处；这里开 resolver 只为兜住误用，不生成 dts（dts:false，
    //   免得多一个触发全量解析的入口）。
    AutoImport({ resolvers: [ElementPlusResolver()], dts: false }),
    Components({ resolvers: [ElementPlusResolver()], dts: false }),
  ],
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
      // 第三十九轮: 智能体接口 —— 同样别落到 vite 自己身上
      '/agents': 'http://127.0.0.1:8808',
    },
  },
  build: {
    // M5: 按需引入后 element-plus 已拆分，超 500kB 的只剩 echarts（服务端大图渲染不可避免）。
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // M5: 拆 vendor chunk，避免第三方打进单个入口超大 chunk。
        manualChunks: {
          'vendor-vue': ['vue', 'vue-router', 'pinia'],
          // 第二十八轮: element-plus 已按需引入——不再把整包塞进一个 chunk；
          //   @element-plus/icons-vue 无用已从依赖移除，这里一并清掉。
          'vendor-echarts': ['echarts'],
          'vendor-markdown': ['marked', 'marked-highlight', 'dompurify', 'highlight.js'],
        },
      },
    },
  },
})