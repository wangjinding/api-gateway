// 导航结构单一来源：桌面侧栏、手机抽屉、手机底部 Tab 都从这里取，
// 避免菜单项加一个地方漏一个地方。分组顺序与设计稿 v2 一致。

import { GATEWAY_CONFIG_ENABLED } from './features'

export interface NavItem {
  path: string
  /** i18n key 后缀：t(`nav.menu${key}`) */
  key: string
  /** Icon.vue 里的图标名 */
  icon: string
}

export interface NavGroup {
  id: string
  items: NavItem[]
}

/** 桌面侧栏 / 手机抽屉的完整菜单（按功能分组，组间一条分隔线）。 */
export function navGroups(isAdmin: boolean): NavGroup[] {
  const groups: NavGroup[] = [
    {
      id: 'core',
      items: [
        { path: '/dashboard', key: 'dashboard', icon: 'dash' },
        { path: '/chat', key: 'chat', icon: 'chat' },
        { path: '/images', key: 'images', icon: 'img' },
        { path: '/videos', key: 'videos', icon: 'vid' },
      ],
    },
    {
      id: 'ops',
      items: [
        { path: '/models', key: 'models', icon: 'model' },
        { path: '/metrics', key: 'metrics', icon: 'metric' },
        { path: '/keys', key: 'keys', icon: 'key' },
      ],
    },
  ]
  const sys: NavItem[] = []
  // 第二十二轮定稿：管理员只处理反馈（「用户反馈」），不提意见 —— 所以这两项互斥。
  if (isAdmin) sys.push({ path: '/users', key: 'users', icon: 'users' })
  if (isAdmin) sys.push({ path: '/feedback-inbox', key: 'feedbackinbox', icon: 'inbox' })
  // 第二十三轮追加：老板要求「网关配置先不要删掉、先隐藏起来，这块先不支持页面配置」→
  // 入口由 `lib/features.ts` 的开关统一控制（页面与路由都还在，只是不挂出来）。
  if (isAdmin && GATEWAY_CONFIG_ENABLED) sys.push({ path: '/gateway-config', key: 'gatewaycfg', icon: 'sliders' })
  sys.push({ path: '/logs', key: 'logs', icon: 'log' })
  if (!isAdmin) sys.push({ path: '/feedback', key: 'feedback', icon: 'bulb' })
  sys.push({ path: '/settings', key: 'settings', icon: 'set' })
  groups.push({ id: 'sys', items: sys })
  return groups
}

/** 手机底部 Tab：四个主功能 + 「更多」（抽屉里是完整菜单）。 */
export const MOBILE_TABS: NavItem[] = [
  { path: '/dashboard', key: 'dashboard', icon: 'dash' },
  { path: '/chat', key: 'chat', icon: 'chat' },
  { path: '/images', key: 'images', icon: 'img' },
  { path: '/videos', key: 'videos', icon: 'vid' },
]
