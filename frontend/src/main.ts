import { createApp } from 'vue'
import { createPinia } from 'pinia'
// 第二十八轮：element-plus 改按需引入（vite.config.ts 的 Components/AutoImport 负责模板里的
//   <el-*>），这里不再 `import ElementPlus` + `app.use(ElementPlus)`、不再引全量 index.css。
//   仍然手动引的只有三样：
//     ① 深色变量基座（A 皮肤用）—— 组件级样式依赖这三个 CSS 变量，必须全局在；
//     ② ElMessage 的样式（Dialogs 里命令式调用，模板里没 <el-message>，resolver 扫不到）；
//     ③ ElMessageBox 的样式（同上，confirm() 弹窗）。
//        ElLoading / ElNotification 本项目未用，无需引（未来要用了记得补）。
import 'element-plus/theme-chalk/dark/css-vars.css' // 深色变量基座（A 皮肤用）
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'highlight.js/styles/github.css'
import App from './App.vue'
import router from './router'
import { initTheme } from './lib/theme'
import i18n from './i18n'
import './style.css'

initTheme() // mount 前应用主题（data-theme + dark class），避免闪色
const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(i18n) // 多语言
app.mount('#app')
