import { createRouter, createWebHashHistory } from 'vue-router'
import { useSessionStore } from './store/session'
import { GATEWAY_CONFIG_ENABLED } from './lib/features'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('./views/Login.vue'), meta: { public: true } },
    {
      path: '/',
      component: () => import('./layouts/MainLayout.vue'),
      children: [
        { path: '', redirect: '/dashboard' },
        { path: 'dashboard', name: 'dashboard', component: () => import('./views/Dashboard.vue'), meta: { title: '仪表盘' } },
        { path: 'chat', name: 'chat', component: () => import('./views/Chat.vue'), meta: { title: '对话' } },
        { path: 'images', name: 'images', component: () => import('./views/Images.vue'), meta: { title: '图像' } },
        { path: 'videos', name: 'videos', component: () => import('./views/Videos.vue'), meta: { title: '视频' } },
        { path: 'models', name: 'models', component: () => import('./views/Models.vue'), meta: { title: '模型与供应商' } },
        { path: 'metrics', name: 'metrics', component: () => import('./views/Metrics.vue'), meta: { title: '指标' } },
        { path: 'keys', name: 'keys', component: () => import('./views/ApiKeys.vue'), meta: { title: 'API 密钥' } },
        { path: 'users', name: 'users', component: () => import('./views/Users.vue'), meta: { title: '用户管理', admin: true } },
        { path: 'feedback-inbox', name: 'feedbackinbox', component: () => import('./views/FeedbackInbox.vue'), meta: { title: '用户反馈', admin: true } },
        { path: 'feedback', name: 'feedback', component: () => import('./views/Feedback.vue'), meta: { title: '意见反馈', memberOnly: true } },
        { path: 'gateway-config', name: 'gatewaycfg', component: () => import('./views/GatewayConfig.vue'), meta: { title: '网关配置', admin: true } },
        { path: 'logs', name: 'logs', component: () => import('./views/Logs.vue'), meta: { title: '日志' } },
        { path: 'settings', name: 'settings', component: () => import('./views/Settings.vue'), meta: { title: '设置' } },
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