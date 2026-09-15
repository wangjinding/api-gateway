<template>
  <div class="app">
    <!-- 宽屏：侧栏常驻 -->
    <aside v-if="!isMobile" class="rail">
      <SideNav :groups="groups" />
    </aside>

    <div class="main">
      <!-- 宽屏顶栏：页面标题 + 副标题 + 连接状态 + 主题 / 刷新 / 通知 / 账号 -->
      <header v-if="!isMobile" class="top">
        <div>
          <h2>{{ pageTitle }}</h2>
          <div class="crumb">{{ pageSub }}</div>
        </div>
        <div class="top-right">
          <div class="host">
            <i class="dot" :class="connOk ? 'ok' : 'bad'"></i>
            <span>{{ connOk ? t('nav.connected') : t('nav.disconnected') }}</span>
          </div>

          <!-- 第二十一轮：tooltip 必须在 dropdown **外面**。反过来（tooltip 包着触发按钮）
               Element Plus 的 ElOnlyChild 会把点击挂到内层 tooltip 的包装节点上，
               真正被点的 <button> 收不到事件 → 菜单永远不开（老板报的「主题点了没反应」）。 -->
          <el-tooltip :content="t('theme.choose')" placement="bottom">
            <el-dropdown trigger="click" @command="onTheme">
              <button class="icon-btn" type="button"><Icon name="theme" :size="16" /></button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item v-for="id in THEME_IDS" :key="id" :command="id" :disabled="theme === id">
                    <span class="t-row"><i class="t-dot" :style="{ background: THEME_META[id].accent }" />{{ t(THEME_META[id].nameKey) }}</span>
                  </el-dropdown-item>
                  <el-dropdown-item divided command="follow">
                    <span class="t-row">
                      <Icon name="check" :size="14" :style="{ opacity: followSystem ? 1 : 0 }" />
                      {{ t('theme.followSystem') }}
                    </span>
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </el-tooltip>

          <el-tooltip :content="t('top.refresh')" placement="bottom">
            <button class="icon-btn" type="button" @click="refresh"><Icon name="refresh" :size="16" /></button>
          </el-tooltip>

          <el-dropdown trigger="click" @command="onCmd">
            <div class="user">
              <div class="avatar">{{ initial }}</div>
              <span>{{ store.user }}<template v-if="store.isAdmin"> · {{ t('common.admin') }}</template></span>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="zh" :disabled="locale === 'zh'">中文</el-dropdown-item>
                <el-dropdown-item command="en" :disabled="locale === 'en'">English</el-dropdown-item>
                <el-dropdown-item divided command="logout">{{ t('common.logout') }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <!-- 窄屏顶栏：汉堡唤出抽屉，右侧只留主题与账号 -->
      <header v-else class="m-top">
        <div class="ic" @click="drawerOpen = true"><Icon name="menu" :size="18" /></div>
        <h3>{{ pageTitle }}</h3>
        <div class="top-right">
          <div class="ic" @click="cycleTheme"><Icon name="theme" :size="18" /></div>
          <el-dropdown trigger="click" @command="onCmd">
            <div class="avatar">{{ initial }}</div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="zh" :disabled="locale === 'zh'">中文</el-dropdown-item>
                <el-dropdown-item command="en" :disabled="locale === 'en'">English</el-dropdown-item>
                <el-dropdown-item divided command="logout">{{ t('common.logout') }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <div class="gw-body">
        <!-- key 里带 refreshTick：点刷新时整页重挂载重新拉数据（页面内部状态清空是有意的） -->
        <router-view :key="route.fullPath + '-' + refreshTick" />
      </div>

      <TabBar v-if="isMobile" @more="drawerOpen = true" />
    </div>

    <!-- 抽屉：完整菜单 + 主题切换；选完菜单立即收起（否则看完页面还被挡着） -->
    <el-drawer v-model="drawerOpen" direction="ltr" :size="252" :with-header="false" class="nav-drawer">
      <div class="drawer-wrap">
        <SideNav :groups="groups" @navigate="drawerOpen = false" />
        <div class="drawer-themes">
          <div class="dt-title">{{ t('theme.choose') }}</div>
          <div class="dt-row">
            <button
              v-for="id in THEME_IDS"
              :key="id"
              type="button"
              class="dt-card"
              :class="{ on: theme === id }"
              @click="onTheme(id)"
            >
              <span class="dt-prev">
                <i class="l" :style="{ background: THEME_META[id].side }" />
                <i class="r" :style="{ background: THEME_META[id].canvas }">
                  <b :style="{ background: THEME_META[id].accent }" />
                  <b :style="{ background: THEME_META[id].accent, opacity: .4, width: '65%' }" />
                </i>
              </span>
              <span class="dt-name">{{ id }}</span>
            </button>
            <button type="button" class="dt-card" :class="{ on: followSystem }" @click="toggleFollow">
              <span class="dt-prev"><Icon name="theme" :size="18" /></span>
              <span class="dt-name">{{ t('theme.followShort') }}</span>
            </button>
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useSessionStore } from '../store/session'
import { api } from '../api'
import { setLocale } from '../i18n'
import SideNav from './SideNav.vue'
import TabBar from './TabBar.vue'
import Icon from '../components/Icon.vue'
import { useIsMobile } from '../lib/ui'
import { navGroups } from '../lib/nav'
import { useTheme, THEME_IDS, THEME_META, type ThemeId } from '../lib/theme'

const route = useRoute()
const router = useRouter()
const store = useSessionStore()
const { t, locale } = useI18n()
const { isMobile } = useIsMobile()
const { theme, followSystem, setTheme, setFollowSystem } = useTheme()

const connOk = ref(true)
const drawerOpen = ref(false)
const refreshTick = ref(0)

const groups = computed(() => navGroups(store.isAdmin))
const initial = computed(() => (store.user || '?').slice(0, 1).toUpperCase())

// 抽屉里选完菜单 / 浏览器前进后退，都收起
watch(() => route.fullPath, () => { drawerOpen.value = false })

const pageTitle = computed(() => {
  const key = String(route.name || '')
  return key ? t(`nav.menu${key}`) : t('nav.control')
})
const pageSub = computed(() => {
  const key = String(route.name || '')
  const sub = t(`nav.sub${key}`)
  // 没写副标题的页面，t() 会把 key 原样返回 —— 这时显示连接地址兜底
  return sub.startsWith('nav.') ? 'api.wangjinding.com' : sub
})

function onTheme(cmd: string | number | object) {
  if (cmd === 'follow') { toggleFollow(); return }
  setTheme(cmd as ThemeId)
}
function toggleFollow() { setFollowSystem(!followSystem.value) }
/** 手机端只有一个图标位：循环 B → A → C（设置页里可以做精细选择） */
function cycleTheme() {
  const i = THEME_IDS.indexOf(theme.value)
  setTheme(THEME_IDS[(i + 1) % THEME_IDS.length])
}
function refresh() { refreshTick.value++ }

async function onCmd(cmd: string) {
  if (cmd === 'logout') {
    await store.logout()
    router.push('/login')
    return
  }
  setLocale(cmd as 'zh' | 'en')
}

let timer: any = null
async function probe() {
  try {
    // 探 /health 而不是 /healthz：后者只回 {"status":"ok"}，不带 uptime，
    // /health 带 version/uptime，顺带把连接状态一起判了（顶栏「连接正常」小灯）。
    const j = await api('/health')
    connOk.value = !!j && j.status === 'ok'
  } catch {
    connOk.value = false
  }
}
onMounted(() => {
  probe()
  timer = setInterval(probe, 10000)
  window.addEventListener('gw-auth-expired', onAuthExpired)
})
onUnmounted(() => {
  clearInterval(timer)
  window.removeEventListener('gw-auth-expired', onAuthExpired)
})

async function onAuthExpired() {
  await store.logout()
  router.push('/login')
}
</script>

<style scoped>
.t-row { display: flex; align-items: center; gap: 8px; }
.t-dot { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.host span { white-space: nowrap; }
/* 抽屉内容：菜单撑满，主题卡固定在底部 */
.drawer-wrap { display: flex; flex-direction: column; height: 100%; background: var(--surface); }
.drawer-themes { flex: none; border-top: 1px solid var(--line); padding: 12px; }
.dt-title { font-size: 11.5px; color: var(--muted-2); margin-bottom: 8px; }
.dt-row { display: flex; gap: 8px; }
.dt-card {
  flex: 1; min-width: 0; border: 1px solid var(--line); background: var(--surface);
  border-radius: 10px; padding: 7px 5px; cursor: pointer; display: flex;
  flex-direction: column; align-items: center; gap: 5px;
}
.dt-card.on { border-color: var(--primary); background: var(--primary-soft); }
.dt-prev { width: 100%; height: 22px; border-radius: 6px; overflow: hidden; display: flex; border: 1px solid var(--line); }
.dt-prev .l { width: 30%; display: block; }
.dt-prev .r { flex: 1; padding: 3px; display: flex; flex-direction: column; gap: 3px; justify-content: center; }
.dt-prev .r b { height: 3px; border-radius: 2px; display: block; }
.dt-name { font-size: 11.5px; color: var(--muted); font-weight: 600; line-height: 22px; }
.dt-card.on .dt-name { color: var(--primary); }
@media (max-width: 768px) {
  .top-right { gap: 6px; }
}
</style>
