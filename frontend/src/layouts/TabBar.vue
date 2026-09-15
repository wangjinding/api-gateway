<template>
  <!-- 手机底部 Tab：4 个主功能 + 「更多」（打开抽屉里的完整菜单）。
       为什么要有「更多」：手机放不下 10 个菜单项，底部 Tab 只留高频入口，
       低频的（模型/指标/密钥/用户/日志/设置）走抽屉，不必牺牲可达性。 -->
  <nav class="tabbar">
    <div
      v-for="tb in tabs"
      :key="tb.path"
      :class="{ on: isActive(tb.path) }"
      @click="go(tb.path)"
    >
      <Icon :name="tb.icon" :size="18" />
      <span>{{ t(`nav.menu${tb.key}`) }}</span>
    </div>
    <div :class="{ on: moreActive }" @click="emit('more')">
      <Icon name="menu" :size="18" />
      <span>{{ t('nav.more') }}</span>
    </div>
  </nav>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import Icon from '../components/Icon.vue'
import { MOBILE_TABS } from '../lib/nav'

const emit = defineEmits<{ (e: 'more'): void }>()
const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const tabs = MOBILE_TABS

function isActive(path: string): boolean {
  return route.path === path || route.path.startsWith(path + '/')
}
/** 当前页不在前 4 个 Tab 里时，「更多」高亮（否则用户不知道自己在哪） */
const moreActive = computed(() => !tabs.some((tb) => isActive(tb.path)))

function go(path: string) {
  if (route.path !== path) router.push(path)
}
</script>
