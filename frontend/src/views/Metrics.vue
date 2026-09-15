<template>
  <div class="page">
    <el-alert v-if="err" :title="err" type="error" show-icon :closable="false" class="mb" />

    <!-- KPI：全部来自 /metrics 当前快照。网关没有历史时序接口，
         所以这里不画趋势条、也不给「近 7 天」这类假的时间筛选。 -->
    <div class="kpis">
      <div v-for="c in cards" :key="c.k" class="kpi">
        <div class="k">{{ c.k }}</div>
        <div class="v">{{ c.v }}</div>
        <div class="f"><span class="muted-2">{{ c.note }}</span></div>
      </div>
    </div>

    <div class="grid-eq">
      <div class="card">
        <div class="card-h">
          <b>{{ t('metrics.modelCalls') }}</b>
          <span class="sub">{{ t('metrics.snapshot') }}</span>
          <div class="legend">
            <span><i style="background: var(--ok)" />{{ t('metrics.success') }}</span>
            <span><i style="background: var(--err)" />{{ t('metrics.fail') }}</span>
          </div>
        </div>
        <EChart :option="callsOpt" height="250px" />
      </div>
      <div class="card">
        <div class="card-h">
          <b>{{ t('metrics.provShare') }}</b>
          <span class="sub">{{ t('metrics.snapshot') }}</span>
        </div>
        <EChart :option="provOpt" height="250px" />
      </div>
    </div>

    <div class="grid-eq">
      <div class="card">
        <div class="card-h">
          <b>{{ t('metrics.routeVsProvider') }}</b>
          <span class="sub">{{ t('metrics.snapshot') }}</span>
          <div class="legend">
            <span><i style="background: var(--chart-a)" />ok</span>
            <span><i style="background: var(--err)" />fail</span>
            <span><i style="background: var(--warn)" />fallback</span>
          </div>
        </div>
        <EChart :option="routeOpt" height="250px" />
      </div>
      <div class="card">
        <div class="card-h">
          <b>{{ t('metrics.latency') }}</b>
          <span class="sub">P50 / P95</span>
          <div class="legend">
            <span><i style="background: var(--chart-a)" />P50</span>
            <span><i style="background: var(--chart-b)" />P95</span>
          </div>
        </div>
        <EChart :option="latOpt" height="250px" />
      </div>
    </div>

    <div class="card" style="margin-top: var(--gap)">
      <div class="card-h">
        <b>{{ t('metrics.circuitStatus') }}</b>
        <span class="sub">{{ t('metrics.circuitCard') }}</span>
      </div>
      <el-table :data="circuits" size="small" v-loading="loading">
        <el-table-column prop="provider" :label="t('metrics.provider')" min-width="140" />
        <el-table-column :label="t('metrics.state')" width="120">
          <template #default="{ row }">
            <span class="pill" :class="stateCls(row.state)">{{ row.state }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="recent_failures" :label="t('metrics.recentFailures')" width="100" />
        <el-table-column prop="window_size" :label="t('metrics.window')" width="90" />
        <el-table-column prop="threshold" :label="t('metrics.threshold')" width="90" />
        <el-table-column prop="cooldown_seconds" :label="t('metrics.cooldown')" width="110" />
        <el-table-column :label="t('metrics.probing')" width="90">
          <template #default="{ row }">
            <Icon v-if="row.probing" name="refresh" :size="14" />
            <span v-else class="muted-2">{{ t('common.no') }}</span>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="card about-card">
      <div class="about-line">
        <b>{{ t('metrics.snapshot') }}</b>
        <span>{{ t('metrics.uptime') }} {{ fmtUptime }}</span>
        <span>{{ t('metrics.fallback') }} {{ data.fallback_total ?? 0 }}</span>
        <span>{{ t('metrics.inflight') }} {{ data.in_flight ?? 0 }} / {{ data.in_flight_peak ?? 0 }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { api } from '../api'
import EChart from '../components/EChart.vue'
import Icon from '../components/Icon.vue'
import { categoryAxis, chartTheme, emptyTip, grid, legend, pieLabel, pieLabelLine, pieRadius, pieCenter, pieData, valueAxis } from '../lib/charts'
import type { EChartsCoreOption } from '../lib/echarts'
import { useTheme } from '../lib/theme'

const router = useRouter()
const { t } = useI18n()
const { resolved } = useTheme()
const data = ref<any>({})
const loading = ref(false)
const err = ref('')
// 运行时间的实时基准：uptimeAt = 上次拿到 /metrics 的时刻，nowTick = 本地每秒心跳。
const uptimeAt = ref(Date.now())
const nowTick = ref(Date.now())

async function load() {
  loading.value = true
  err.value = ''
  try {
    data.value = await api('/metrics')
    uptimeAt.value = Date.now()
  } catch (e: any) {
    if (e && e.status === 401) { router.push('/login'); return }
    err.value = e?.message || String(e)
  } finally { loading.value = false }
}

const fmtUptime = computed(() => {
  const u = data.value.uptime_seconds
  if (u == null) return '-'
  // 运行时间要实时走：/metrics 是 8s 一拉，直接用返回值会「8 秒跳一格」。
  // 这里以拿到值的那一刻为基准本地每秒累加，秒针是连续走的。
  void nowTick.value
  const live = u + Math.max(0, (nowTick.value - uptimeAt.value) / 1000)
  const d = Math.floor(live / 86400); const h = Math.floor((live % 86400) / 3600)
  const mo = Math.floor((live % 3600) / 60); const s = Math.floor(live % 60)
  return d ? `${d}d ${h}h ${mo}m ${s}s` : h ? `${h}h ${mo}m ${s}s` : `${mo}m ${s}s`
})

const totals = computed(() => {
  const c = data.value.calls || {}
  let ok = 0, fail = 0
  for (const m of Object.values(c) as any[]) { ok += m?.ok || 0; fail += m?.fail || 0 }
  return { ok, fail, all: ok + fail }
})

const cards = computed(() => {
  void resolved.value
  const tk = data.value.tokens || {}
  const { ok, fail, all } = totals.value
  const rate = all ? (fail / all) * 100 : 0
  return [
    { k: t('metrics.requests'), v: all, note: t('dashboard.noteOkFail', { ok, fail }) },
    { k: t('metrics.failCount'), v: fail, note: t('metrics.failRateNote', { v: rate.toFixed(1) }) },
    { k: t('metrics.tokenUsage'), v: (tk.prompt || 0) + (tk.completion || 0), note: t('metrics.tokenNote', { p: tk.prompt ?? 0, c: tk.completion ?? 0 }) },
    { k: t('metrics.inflightPeak'), v: `${data.value.in_flight ?? 0} / ${data.value.in_flight_peak ?? 0}`, note: t('metrics.fallback') + ' ' + (data.value.fallback_total ?? 0) },
  ]
})

const callsOpt = computed<EChartsCoreOption>(() => {
  void resolved.value
  const c = chartTheme()
  const calls = data.value.calls || {}
  const models = Object.keys(calls)
  const ok = models.map((m) => calls[m]?.ok || 0)
  const fail = models.map((m) => calls[m]?.fail || 0)
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: legend([t('metrics.success'), t('metrics.fail')]),
    grid,
    graphic: emptyTip([ok, fail], t('metrics.none')),
    xAxis: categoryAxis(models),
    yAxis: valueAxis('', 1),
    series: [
      { name: t('metrics.success'), type: 'bar', stack: 'c', itemStyle: { color: c.ok }, data: ok },
      { name: t('metrics.fail'), type: 'bar', stack: 'c', itemStyle: { color: c.err }, data: fail },
    ],
  }
})

const provOpt = computed<EChartsCoreOption>(() => {
  void resolved.value
  const p = data.value.providers || {}
  const items = Object.keys(p).map((k) => ({ name: k, value: p[k]?.ok || 0 })).filter((x) => x.value > 0)
  return {
    tooltip: { trigger: 'item' },
    series: [{
      type: 'pie', radius: pieRadius, center: pieCenter, minAngle: 6, labelLine: pieLabelLine,
      data: pieData(items, t('metrics.none')), label: pieLabel(), itemStyle: { borderRadius: 4 },
    }],
  }
})

const routeOpt = computed<EChartsCoreOption>(() => {
  void resolved.value
  const c = chartTheme()
  const r = data.value.routes || {}
  const ks = Object.keys(r)
  const names = ks.length ? ks : ['wjd']
  const get = (k: string, f: string) => (r[k] || {})[f] || 0
  const ok = names.map((n) => get(n, 'ok'))
  const fail = names.map((n) => get(n, 'fail'))
  const fallback = names.map((n) => get(n, 'fallback'))
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: legend(['ok', 'fail', 'fallback']),
    grid,
    graphic: emptyTip([ok, fail, fallback], t('metrics.none')),
    xAxis: categoryAxis(names, 10),
    yAxis: valueAxis('', 1),
    series: [
      { name: 'ok', type: 'bar', stack: 'r', data: ok, itemStyle: { color: c.a } },
      { name: 'fail', type: 'bar', stack: 'r', data: fail, itemStyle: { color: c.err } },
      { name: 'fallback', type: 'bar', stack: 'r', data: fallback, itemStyle: { color: c.warn } },
    ],
  }
})

const latOpt = computed<EChartsCoreOption>(() => {
  void resolved.value
  const c = chartTheme()
  const l = data.value.latency || {}
  const models = Object.keys(l)
  const p50 = models.map((m) => l[m]?.p50 ?? 0)
  const p95 = models.map((m) => l[m]?.p95 ?? 0)
  return {
    tooltip: { trigger: 'axis', valueFormatter: (v: any) => `${typeof v === 'number' ? v.toFixed(3) : v}s` },
    legend: legend(['P50', 'P95']),
    grid,
    graphic: emptyTip([p50, p95], t('metrics.none')),
    xAxis: categoryAxis(models),
    yAxis: valueAxis('s'),
    series: [
      { name: 'P50', type: 'bar', data: p50, itemStyle: { color: c.a, borderRadius: [4, 4, 0, 0] } },
      { name: 'P95', type: 'bar', data: p95, itemStyle: { color: c.b, borderRadius: [4, 4, 0, 0] } },
    ],
  }
})

const circuits = computed(() => data.value.circuits || [])
function stateCls(s: string) {
  const v = String(s || '').toUpperCase()
  return v === 'CLOSED' ? 'ok' : v === 'HALF_OPEN' ? 'warn' : 'err'
}

let timer: any = null
let tickTimer: any = null
onMounted(() => {
  load()
  timer = setInterval(load, 8000)
  tickTimer = setInterval(() => { nowTick.value = Date.now() }, 1000)
})
onUnmounted(() => { clearInterval(timer); clearInterval(tickTimer) })
</script>

<style scoped>
.about-card { margin-top: var(--gap); padding: 12px 16px; }
.about-line b { color: var(--text); }
</style>
