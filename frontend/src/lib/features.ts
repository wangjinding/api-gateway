// 功能开关（第二十三轮追加，老板 19:3x）
//
// 老板原话：「网关配置功能先不要删掉先隐藏起来，这块先不支持页面配置」。
// 所以这里**不是删功能**：`views/GatewayConfig.vue`、路由定义、中英文案、后端 `GET|PUT /config`
// 全部原样保留，只把下面这个开关关掉 ——
//   ① 桌面侧栏与手机抽屉不出现「网关配置」入口（`lib/nav.ts`）
//   ② 设置页那颗「打开配置页」按钮不渲染（`views/Settings.vue`）
//   ③ 直接手敲 / 地址栏输入 `#/gateway-config` 会被路由守卫送回仪表盘（`router.ts`）
// 恢复 = 把这一行改成 `true`，三个入口同时回来（后端接口本来就没停）。
export const GATEWAY_CONFIG_ENABLED = true   // 老板 2026-09-15 重新开放：整页 YAML 编辑器 + API Key 环境变量管理
