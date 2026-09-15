/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

// 第十三轮: element-plus 的语言包是 .mjs 且不带类型声明，vue-tsc 会报 TS7016。
// 只是两个纯数据模块（locale 对象），补声明让类型检查保持干净（App.vue 用到）。
declare module 'element-plus/dist/locale/zh-cn.mjs'
declare module 'element-plus/dist/locale/en.mjs'
