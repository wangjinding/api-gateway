import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css' // 深色变量基座（A 皮肤用）
import 'highlight.js/styles/github.css'
import * as ElementIcons from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'
import { initTheme } from './lib/theme'
import i18n from './i18n'
import './style.css'

initTheme() // mount 前应用主题（data-theme + dark class），避免闪色
const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(ElementPlus)
app.use(i18n) // 多语言
for (const [name, comp] of Object.entries(ElementIcons)) {
  app.component(name as string, comp as any)
}
app.mount('#app')
