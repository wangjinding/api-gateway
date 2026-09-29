<template>
  <!-- 线性几何图标：跨设备字形不变（emoji 在 Windows/Android 上长得完全不一样）。 -->
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 20 20"
    fill="none"
    stroke="currentColor"
    :stroke-width="stroke"
    stroke-linecap="round"
    stroke-linejoin="round"
    class="gw-icon"
    aria-hidden="true"
    v-html="body"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{ name: string; size?: number; stroke?: number }>(), {
  size: 18,
  stroke: 1.7,
})

// 图标表：键为语义名，值为 20×20 视框内的路径。与设计稿同一套几何。
const ICONS: Record<string, string> = {
  // 第三十八轮：智能体（与设计稿 design/v3/agents.py 的 i-bot 同几何）
  bot: '<rect x="3.6" y="6.4" width="12.8" height="9.2" rx="2.6"/><path d="M10 3.2v3.2"/><circle cx="10" cy="3" r="1.1"/><circle cx="7.4" cy="10.6" r="1.15" fill="currentColor" stroke="none"/><circle cx="12.6" cy="10.6" r="1.15" fill="currentColor" stroke="none"/><path d="M8.2 13.4h3.6"/>',
  dash: '<rect x="3" y="3" width="6.2" height="6.2" rx="1.8"/><rect x="10.8" y="3" width="6.2" height="6.2" rx="1.8"/><rect x="3" y="10.8" width="6.2" height="6.2" rx="1.8"/><rect x="10.8" y="10.8" width="6.2" height="6.2" rx="1.8"/>',
  chat: '<path d="M3.5 6.2A2.7 2.7 0 0 1 6.2 3.5h7.6a2.7 2.7 0 0 1 2.7 2.7v4.6a2.7 2.7 0 0 1-2.7 2.7H8.6L4.7 16.2v-2.9a2.7 2.7 0 0 1-1.2-2.2z"/>',
  img: '<rect x="3" y="4" width="14" height="12" rx="2.4"/><circle cx="7.4" cy="8.4" r="1.3"/><path d="M3.6 14.4l4-3.6 2.8 2.4 2.9-2.6 3.1 2.8"/>',
  vid: '<rect x="2.6" y="5" width="14.8" height="10" rx="2.4"/><path d="M8.6 8l3.6 2-3.6 2z"/>',
  model: '<path d="M10 3l6.6 3.6L10 10.2 3.4 6.6z"/><path d="M3.4 10.6L10 14.2l6.6-3.6"/><path d="M3.4 14L10 17.6 16.6 14"/>',
  metric: '<path d="M4 16.4V10"/><path d="M8 16.4V4.6"/><path d="M12 16.4v-4.2"/><path d="M16 16.4V7.4"/>',
  key: '<circle cx="6.6" cy="10" r="3.1"/><path d="M9.4 10h7"/><path d="M14.6 10v2.6"/><path d="M16.6 10v2"/>',
  users: '<circle cx="8" cy="7.4" r="2.9"/><path d="M3.4 16.2c0-2.6 2-4.2 4.6-4.2s4.6 1.6 4.6 4.2"/><path d="M13.6 5.5a2.9 2.9 0 0 1 0 5.2"/><path d="M14.6 12.4c1.7.6 2.9 1.9 2.9 3.8"/>',
  log: '<rect x="3.4" y="3.4" width="13.2" height="13.2" rx="2.4"/><path d="M6.6 8h6.8"/><path d="M6.6 11h6.8"/><path d="M6.6 14h4"/>',
  set: '<path d="M3.6 6.2h12.8"/><path d="M3.6 10h12.8"/><path d="M3.6 13.8h12.8"/><circle cx="7.4" cy="6.2" r="1.7"/><circle cx="13" cy="10" r="1.7"/><circle cx="8.8" cy="13.8" r="1.7"/>',
  menu: '<path d="M3.6 6h12.8"/><path d="M3.6 10h12.8"/><path d="M3.6 14h12.8"/>',
  refresh: '<path d="M16.2 8.6A6.4 6.4 0 1 0 16 12.6"/><path d="M16.6 3.6v4.4h-4.4"/>',
  dl: '<path d="M10 3.6v8"/><path d="M6.6 8.8L10 12.2l3.4-3.4"/><path d="M4.4 15.8h11.2"/>',
  bell: '<path d="M10 3.4A4.4 4.4 0 0 1 14.4 8c0 3 .8 4 1.3 4.6H4.3C4.8 12 5.6 11 5.6 8A4.4 4.4 0 0 1 10 3.4z"/><path d="M8.4 15.6a1.7 1.7 0 0 0 3.2 0"/>',
  plus: '<path d="M10 4.6v10.8"/><path d="M4.6 10h10.8"/>',
  up: '<path d="M10 15.6V4.8"/><path d="M5.6 9.2L10 4.8l4.4 4.4"/>',
  theme: '<circle cx="10" cy="10" r="6.8"/><circle cx="7.4" cy="8.2" r="1.05" fill="currentColor"/><circle cx="12.6" cy="8.2" r="1.05" fill="currentColor"/><circle cx="10" cy="12.8" r="1.05" fill="currentColor"/>',
  play: '<circle cx="10" cy="10" r="7"/><path d="M8.4 7.4l4.6 2.6-4.6 2.6z"/>',
  eye: '<path d="M2.6 10S5.4 5.4 10 5.4 17.4 10 17.4 10 14.6 14.6 10 14.6 2.6 10 2.6 10z"/><circle cx="10" cy="10" r="2.2"/>',
  bolt: '<path d="M11.6 2.2 4.8 11.1h4.3L8.7 17.8l6.9-9.3h-4.4z"/>',
  send: '<path d="M17 3 3.4 8.8l4.9 2.3L11 16.8z"/><path d="M17 3l-8.7 8.1"/>',
  chev: '<path d="M7.6 4.6 13 10l-5.4 5.4"/>',
  chevD: '<path d="M4.6 7.6 10 13l5.4-5.4"/>',
  clip: '<path d="M13.4 8.6 8 14a2.6 2.6 0 0 1-3.7-3.7l7-7a1.8 1.8 0 0 1 2.6 2.6l-7 7"/>',
  x: '<path d="M5.2 5.2l9.6 9.6"/><path d="M14.8 5.2l-9.6 9.6"/>',
  check: '<path d="M4.6 10.6l3.4 3.4 7.4-8"/>',
  copy: '<rect x="7" y="7" width="9.4" height="9.4" rx="2"/><path d="M13 4.4a1.8 1.8 0 0 0-1.8-1.8H5.4A1.8 1.8 0 0 0 3.6 4.4v5.8A1.8 1.8 0 0 0 5.4 12"/>',
  trash: '<path d="M4.4 6h11.2"/><path d="M8 6V4.6a1.2 1.2 0 0 1 1.2-1.2h1.6A1.2 1.2 0 0 1 12 4.6V6"/><path d="M5.8 6l.7 9.2a1.4 1.4 0 0 0 1.4 1.3h4.2a1.4 1.4 0 0 0 1.4-1.3L14.2 6"/>',
  search: '<circle cx="9" cy="9" r="5.2"/><path d="M12.9 12.9 17 17"/>',
  user: '<circle cx="10" cy="7" r="3.2"/><path d="M4 17c0-3 2.7-4.8 6-4.8s6 1.8 6 4.8"/>',
  link: '<path d="M8.6 11.4a3 3 0 0 0 4.2 0l2.2-2.2a3 3 0 0 0-4.2-4.2L9.6 6.2"/><path d="M11.4 8.6a3 3 0 0 0-4.2 0L5 10.8a3 3 0 0 0 4.2 4.2l1.2-1.2"/>',
  stop: '<rect x="5.6" y="5.6" width="8.8" height="8.8" rx="2"/>',
  filter: '<path d="M3.6 5.4h12.8"/><path d="M6.4 10h7.2"/><path d="M8.6 14.6h2.8"/>',
  dots: '<circle cx="5" cy="10" r="1.2" fill="currentColor"/><circle cx="10" cy="10" r="1.2" fill="currentColor"/><circle cx="15" cy="10" r="1.2" fill="currentColor"/>',
  // 第二十二轮：意见反馈（灯泡＝想法）/ 用户反馈（收件盘＝收到的意见）
  bulb: '<path d="M10 2.8a5 5 0 0 0-3 9v2.2h6V11.8a5 5 0 0 0-3-9z"/><path d="M8.2 16.6h3.6"/><path d="M8.8 18.4h2.4"/>',
  // 第二十三轮：网关配置（滑杆 = 调参数）
  sliders: '<path d="M3.6 6.8h5.6"/><path d="M12.6 6.8h3.8"/><circle cx="10.9" cy="6.8" r="1.7"/><path d="M3.6 13.2h3.8"/><path d="M10.8 13.2h5.6"/><circle cx="9.1" cy="13.2" r="1.7"/>',
  inbox: '<path d="M3.4 11.6 6 4.6h8l2.6 7v4.8H3.4z"/><path d="M3.4 11.6h3.2l.9 1.8h5l.9-1.8h3.2"/>',
  // 第二十四轮：账号停用（禁用符号「圆 + 斜杠」），启用配已有的 play（圆 + 三角）
  ban: '<circle cx="10" cy="10" r="7"/><path d="M5.1 5.1l9.8 9.8"/>',
  // 第二十五轮：模型参数被上游固定（挂锁 = 这参数你说了不算，由网关按上游要求锁定）
  lock: '<rect x="4.6" y="9" width="10.8" height="7.2" rx="2"/><path d="M7 9V6.8a3 3 0 0 1 6 0V9"/>',
}

const body = computed(() => ICONS[props.name] || '')
</script>

<style scoped>
.gw-icon { display: block; flex: none; }
</style>
