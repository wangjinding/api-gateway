<template>
  <div ref="el" class="echart" :style="{ height: height }"></div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { echarts, type EChartsCoreOption } from '../lib/echarts'

const props = defineProps<{
  option: EChartsCoreOption
  height?: string
}>()

const el = ref<HTMLElement>()
let chart: any = null
let ro: ResizeObserver | null = null

function setOpt() {
  if (chart) chart.setOption(props.option || {}, true)
}
function resize() { if (chart) chart.resize() }

onMounted(() => {
  chart = echarts.init(el.value!)
  setOpt()
  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(resize)
    ro.observe(el.value!)
  }
})
watch(() => props.option, setOpt, { deep: true })
onUnmounted(() => {
  if (ro) ro.disconnect()
  if (chart) { chart.dispose(); chart = null }
})
</script>

<style scoped>
.echart { width: 100%; }
</style>