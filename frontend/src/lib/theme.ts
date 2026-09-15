// 主题管理（前端重设计 v2）。
//
// 口径：一套结构 + 三套 token。切主题只改 <html data-theme>，页面代码零改动。
//   A 深空蓝（深色）· B 素白蓝紫（浅色，默认）· C 暖砂墨绿（浅色暖调）
//
// 为什么还要切 <html class="dark">：
//   Element Plus 的弹层/表单深度配色由 dark/css-vars.css（html.dark）驱动，
//   A 是深色皮肤，必须同时打开这个开关；B/C 关掉。
//   两者不冲突 —— 具体的 --el-* 值在 style.css 里由 :root[data-theme] 覆盖。
//
// 「跟随系统深色模式」：系统切深色时自动用 A，切回浅色时用用户选的主题。
import { computed, ref } from 'vue'

export type ThemeId = 'A' | 'B' | 'C'

export const THEME_IDS: ThemeId[] = ['A', 'B', 'C']
/** 默认皮肤：B 素白蓝紫（老板拍板） */
export const DEFAULT_THEME: ThemeId = 'B'

const THEME_KEY = 'gw_theme_v2'
const FOLLOW_KEY = 'gw_follow_system'
const LEGACY_KEY = 'gw_theme' // 旧版只存 light / dark

const DARK_QUERY = '(prefers-color-scheme: dark)'

function readThemeId(): ThemeId {
  try {
    const v = localStorage.getItem(THEME_KEY)
    if (v === 'A' || v === 'B' || v === 'C') return v
    // 老用户的偏好迁移：深色 → A，浅色 → B（新默认）
    const legacy = localStorage.getItem(LEGACY_KEY)
    if (legacy === 'dark') return 'A'
    return DEFAULT_THEME
  } catch {
    return DEFAULT_THEME
  }
}

function readFollow(): boolean {
  try {
    return localStorage.getItem(FOLLOW_KEY) === '1'
  } catch {
    return false
  }
}

function systemPrefersDark(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false
  return window.matchMedia(DARK_QUERY).matches
}

// 模块级单例：顶栏调色盘、设置页主题卡、手机「更多」抽屉三处共用同一份状态
const themeId = ref<ThemeId>(readThemeId())
const followSystem = ref<boolean>(readFollow())
const systemDark = ref<boolean>(systemPrefersDark())

/** 实际生效的皮肤（跟随系统时可能是 A） */
const resolved = computed<ThemeId>(() => (followSystem.value && systemDark.value ? 'A' : themeId.value))

/** 把当前皮肤写进 <html>：data-theme 喂自定义 token，dark class 喂 Element Plus */
function apply(): void {
  const id = resolved.value
  const root = document.documentElement
  root.setAttribute('data-theme', id)
  root.classList.toggle('dark', id === 'A')
}

let started = false
/** 在 mount 前调用一次：立即上色 + 监听系统深浅色变化（避免闪白/闪黑）。 */
export function initTheme(): void {
  try {
    apply()
  } catch { /* 忽略 */ }
  if (started || typeof window === 'undefined' || !window.matchMedia) return
  started = true
  const mql = window.matchMedia(DARK_QUERY)
  const onChange = (e: MediaQueryListEvent) => {
    systemDark.value = e.matches
    apply()
  }
  if (typeof mql.addEventListener === 'function') mql.addEventListener('change', onChange)
  else mql.addListener(onChange)
}

export function getThemeId(): ThemeId {
  return themeId.value
}

export function setThemeId(id: ThemeId): void {
  themeId.value = id
  try {
    localStorage.setItem(THEME_KEY, id)
  } catch { /* 忽略 */ }
  apply()
}

export function isFollowSystem(): boolean {
  return followSystem.value
}

export function setFollowSystem(on: boolean): void {
  followSystem.value = on
  try {
    localStorage.setItem(FOLLOW_KEY, on ? '1' : '0')
  } catch { /* 忽略 */ }
  apply()
}

/** 组件里用的响应式主题状态。 */
export function useTheme() {
  return {
    theme: computed(() => themeId.value),
    resolved,
    followSystem: computed(() => followSystem.value),
    setTheme: setThemeId,
    setFollowSystem,
  }
}

// 名称/说明走 i18n（key 见 locales/zh.ts · theme.nameA 等）：换语言时皮肤名跟着变，
// 这里只留「结构色值」——那些是设计常量，不该被翻译。
export const THEME_META: Record<ThemeId, { nameKey: string; descKey: string; side: string; canvas: string; accent: string }> = {
  B: { nameKey: 'theme.nameB', descKey: 'theme.descB', side: '#FFFFFF', canvas: '#F4F6FB', accent: '#4F63F5' },
  A: { nameKey: 'theme.nameA', descKey: 'theme.descA', side: '#121B2C', canvas: '#0A101E', accent: '#35D0BA' },
  C: { nameKey: 'theme.nameC', descKey: 'theme.descC', side: '#FFFFFF', canvas: '#F8F4EF', accent: '#0E7C6B' },
}
