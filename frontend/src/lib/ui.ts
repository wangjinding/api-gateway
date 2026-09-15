// 移动端适配的单一断点来源。
//
// 为什么用 matchMedia 而不是监听 resize 比 innerWidth：
//   平板转屏、桌面浏览器缩放都能可靠触发，且与 style.css 里的
//   @media (max-width: 768px) 天然对齐 —— 样式断点交给 CSS，
//   「侧栏 vs 抽屉」这类**结构**切换才需要 JS。改断点时两处必须一起改。
import { onUnmounted, ref } from 'vue'

export const MOBILE_MAX = 768
const QUERY = `(max-width: ${MOBILE_MAX}px)`

/** 响应式「是否窄屏」。组件卸载时自动摘掉监听。 */
export function useIsMobile() {
  const isMobile = ref(typeof window !== 'undefined' && !!window.matchMedia
    ? window.matchMedia(QUERY).matches
    : false)

  if (typeof window === 'undefined' || !window.matchMedia) return { isMobile }

  const mql = window.matchMedia(QUERY)
  const onChange = (e: MediaQueryListEvent) => { isMobile.value = e.matches }
  if (typeof mql.addEventListener === 'function') {
    mql.addEventListener('change', onChange)
    onUnmounted(() => mql.removeEventListener('change', onChange))
  } else {
    // Safari < 14 只有已废弃的 addListener
    const legacy = mql as unknown as { addListener(cb: (e: MediaQueryListEvent) => void): void; removeListener(cb: (e: MediaQueryListEvent) => void): void }
    legacy.addListener(onChange)
    onUnmounted(() => legacy.removeListener(onChange))
  }
  return { isMobile }
}
