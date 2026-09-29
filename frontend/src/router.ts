import { createRouter, createWebHashHistory } from 'vue-router'
import { useSessionStore } from './store/session'
import { GATEWAY_CONFIG_ENABLED } from './lib/features'

const router = createRouter({
  history: createWebHashHistory(),
  // 第三十轮: 这里的 meta.title 已删除 —— 它是**没人读的死元数据**（全仓没有任何
  //   ``document.title``/``route.meta.title`` 消费点），而且只有中文：切英文后一旦有人
  //   开始用它，界面就会冒出中文。菜单标题的唯一来源是 lib/nav.ts（键）+ i18n 文案。
  routes: [
    { path: '/login', name: 'login', component: () => import('./views/Login.vue'), meta: { public: true } },
    {
      path: '/',
      component: () => import('./layouts/MainLayout.vue'),
      children: [
        { path: '', redirect: '/dashboard' },
        { path: 'dashboard', name: 'dashboard', component: () => import('./views/Dashboard.vue') },
        { path: 'chat', name: 'chat', component: () => import('./views/Chat.vue') },
        { path: 'images', name: 'images', component: () => import('./views/Images.vue') },
        { path: 'videos', name: 'videos', component: () => import('./views/Videos.vue') },
        { path: 'models', name: 'models', component: () => import('./views/Models.vue') },
        // 第三十八轮：智能体（一期仅管理员；用 meta.admin 做双层守卫）
        { path: 'agents', name: 'agents', component: () => import('./views/Agents.vue'), meta: { admin: true, agents: true } },
        { path: 'metrics', name: 'metrics', component: () => import('./views/Metrics.vue') },
        { path: 'keys', name: 'keys', component: () => import('./views/ApiKeys.vue') },
        { path: 'users', name: 'users', component: () => import('./views/Users.vue'), meta: { admin: true } },
        { path: 'feedback-inbox', name: 'feedbackinbox', component: () => import('./views/FeedbackInbox.vue'), meta: { admin: true } },
        { path: 'feedback', name: 'feedback', component: () => import('./views/Feedback.vue'), meta: { memberOnly: true } },
        { path: 'gateway-config', name: 'gatewaycfg', component: () => import('./views/GatewayConfig.vue'), meta: { admin: true } },
        { path: 'logs', name: 'logs', component: () => import('./views/Logs.vue') },
        { path: 'settings', name: 'settings', component: () => import('./views/Settings.vue') },
      ],
    },
  ],
})

router.beforeEach((to) => {
  const s = useSessionStore()
  if (!to.meta.public && !s.isAuthed) return '/login'
  if (to.path === '/login' && s.isAuthed) return '/dashboard'
  // 仅管理员的页面（用户管理）：普通账号手敲地址也别进去 ——
  // 侧栏/抽屉里本来就不显示，进去只会看到一个 403 空表和「创建账号」按钮。
  if (to.meta.admin && !s.isAdmin) return '/dashboard'
  // 反向同理（第二十二轮）：管理员只处理、不提意见反馈，手敲 /feedback 也送回收件箱。
  if (to.meta.memberOnly && s.isAdmin) return '/feedback-inbox'
  // 第二十三轮追加：网关配置页暂时隐藏（老板「不删、先藏起来」）→ 手敲地址也送回仪表盘。
  // 路由与组件都留着，把 `lib/features.ts` 的开关打开即恢复。
  if (to.name === 'gatewaycfg' && !GATEWAY_CONFIG_ENABLED) return '/dashboard'
  return true
})

export default router