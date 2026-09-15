<template>
  <!-- M5+: Element Plus 组件语言包跟随界面语言（分页/日期等文案中英对齐） -->
  <el-config-provider :locale="elLocale">
    <el-config-provider :z-index="3000">
      <router-view />
    </el-config-provider>
  </el-config-provider>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import zhCn from 'element-plus/dist/locale/zh-cn.mjs'
import en from 'element-plus/dist/locale/en.mjs'

const { locale } = useI18n()
const elLocale = computed(() => (locale.value === 'en' ? en : zhCn))
</script>