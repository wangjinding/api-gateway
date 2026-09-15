<template>
  <div class="page">
    <div class="card">
      <div class="ph-title">「{{ title }}」页面</div>
      <div class="ph-sub">该功能在后续里程碑开发（M2 对话 / M3 媒体 / M4 指标日志 等）。</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
const route = useRoute()
const title = computed(() => (route.meta.title as string) || '')
</script>

<style scoped>
.ph-title { font-size: 18px; font-weight: 600; }
.ph-sub { color: var(--muted); margin-top: 8px; }
</style>