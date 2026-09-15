<template>
  <div class="page">
    <el-alert v-if="err" :title="err" type="error" show-icon :closable="false" class="mb" />

    <!-- KPI：全部来自 /metrics + /health 的真实计数，没有可得的时序就不画趋势条 -->
    <div class="kpis">
      <div v-for="c in cards" :key="c.k" class="kpi">
        <div class="k">{{ c.k }}</div>
        <div class="v">{{ c.v }}</div>
        <div class="f"><span class="muted-2">{{ c.note }}</span></div>
      </div>
    </div>

    <div class="grid2">
      <div class="card">
        <div class="card-h">
          <b>{{ t('dashboard.latency') }}</b>
          <span class="sub">P50 / P95</span>
          <div class="legend">
            <span><i style="background: var(--chart-a)" />P50</span>
            <span><i style="background: var(--chart-b)" />P95</span>
          </div>
        </div>
        <EChart :option="latOpt" height="228px" />
      </div>

      <div class="card">
        <div class="card-h">
          <b>{{ t('dashboard.providerStatus') }}</b>
          <span class="sub">{{ t('dashboard.circuit') }}</span>
        </div>
        <div class="pv">
          <div v-for="p in providerRows" :key="p.name" class="pv-i">
            <span class="nm">{{ p.name }}</span>
            <span class="meter" style="width: 70px"><i :style="{ width: p.ratio + '%' }" /></span>
            <span class="st"><span class="pill" :class="p.cls">{{ p.text }}</span></span>
          </div>
          <div v-if="!providerRows.length" class="hint">{{ t('dashboard.noData') }}</div>
        </div>

        <div class="sep" />

        <div class="card-h" style="margin-bottom: 8px">
          <b>{{ t('dashboard.routeAlias') }}</b>
          <span class="sub">{{ t('dashboard.routeChain') }}</span>
        </div>
        <div class="pv" style="gap: 7px">
          <div v-for="(r, i) in routeRows" :key="r.name" class="pv-i" style="font-size: 12px">
            <span class="pill mute">{{ i + 1 }}</span>
            <span class="code flex-1">{{ r.name }}</span>
            <span class="st muted-2 nowrap">{{ r.short }}</span>
          </div>
          <div v-if="!routeRows.length" class="hint">{{ t('dashboard.noData') }}</div>
        </div>
      </div>
    </div>

    <div class="grid3">
      <div class="card">
        <div class="card-h">
          <b>{{ t('dashboard.modelCalls') }}</b>
          <span class="sub">{{ t('dashboard.live') }}</span>
        </div>
        <el-table :data="callRows" size="small" v-loading="loading" :show-header="true">
          <el-table-column prop="model" :label="t('dashboard.colModel')" min-width="170">
            <template #default="{ row }">
              <span class="code">{{ row.model }}</span>
              <div class="hint">{{ row.provider }}</div>
            </template>
          </el-table-column>
          <el-table-column prop="calls" :label="t('dashboard.colCalls')" width="80" />
          <el-table-column prop="latency" :label="t('dashboard.colLatency')" width="96" />
          <el-table-column :label="t('dashboard.colRate')" width="92">
            <template #default="{ row }">
              <span class="pill" :class="row.rateCls">{{ row.rate }}</span>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="!callRows.length && !loading" class="hint" style="padding: 8px 0">{{ t('dashboard.noModels') }}</div>
      </div>

      <div class="card">
        <div class="card-h">
          <b>{{ t('dashboard.provShare') }}</b>
          <span class="sub">{{ t('dashboard.share') }}</span>
        </div>
        <EChart :option="provOpt" height="228px" />
      </div>
    </div>

    <div class="card about-card">
      <div class="about-line">
        <b>{{ t('dashboard.gatewayInfo') }} · v{{ health.version || '-' }}</b>
        <span>{{ t('dashboard.running') }}</span>
        <span>{{ t('dashboard.uptime') }} {{ uptimeText }}</span>
        <span>{{ t('dashboard.inflight') }} {{ health.in_flight ?? '-' }} / {{ health.in_flight_peak ?? '-' }}</span>
        <span>{{ t('dashboard.fallback') }} {{ health.fallback_total ?? 0 }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { api } from '../api'
import EChart from '../components/EChart.vue'
import { categoryAxis, chartTheme, emptyTip, grid, legend, pieLabel, pieLabelLine, pieRadius, pieCenter, pieData, valueAxis } from '../lib/charts'
import type { EChartsCoreOption } from '../lib/echarts'
import { useTheme } from '../lib/theme'

const router = useRouter()
const { t } = useI18n()
const { resolved } = useTheme() // 主题变化时重算图表配色
const health = ref<any>({})
const metrics = ref<any>({})
const models = ref<any>({})
const routes = ref<any[]>([])
const loading = ref(false)
const err = ref('')

const textModels = ref<Record<string, string>>({})

function sum(list: any[], key: string): number {
  // 熔断返回的是 ProviderStat 对象，键缺失时按 0 处理
  return list.reduce((acc, x) => acc + (Number(x && x[key]) || 0), 0)
}
const calls = computed(() => metrics.value.calls || {})
const totalOk = computed(() => sum(Object.values(calls.value) as any[], 'ok'))
const totalFail = computed(() => sum(Object.values(calls.value) as any[], 'fail'))
const total = computed(() => totalOk.value + totalFail.value)
const rate = computed(() => (total.value ? (totalOk.value / total.value) * 100 : 100))

const latency = computed<any>(() => metrics.value.latency || {})
const avgP50 = computed(() => {
  const vals = Object.values(latency.value) as any[]
  if (!vals.length) return 0
  return vals.reduce((a, x) => a + (Number(x?.p50) || 0), 0) / vals.length
})
const maxP95 = computed(() => {
  const vals = Object.values(latency.value) as any[]
  return vals.reduce((a, x) => Math.max(a, Number(x?.p95) || 0), 0)
})

const cards = computed(() => {
  void resolved.value // 触发依赖，主题切换时 KPI 也重算（颜色走 CSS，无需改值）
  return [
    { k: t('dashboard.requests'), v: total.value, note: t('dashboard.noteOkFail', { ok: totalOk.value, fail: totalFail.value }) },
    { k: t('dashboard.successRate'), v: rate.value.toFixed(1) + '%', note: t('dashboard.noteFail', { n: totalFail.value }) },
    { k: t('dashboard.avgLatency'), v: avgP50.value.toFixed(2) + 's', note: t('dashboard.noteP95', { v: maxP95.value.toFixed(2) }) },
    { k: t('dashboard.fallbackShort'), v: health.value.fallback_total ?? 0, note: t('dashboard.noteInflight', { n: health.value.in_flight ?? 0 }) },
  ]
})

const providerRows = computed(() => {
  const p = health.value.providers || {}
  return Object.keys(p).map((k) => {
    const c = String(p[k].circuit || '').toUpperCase()
    const cls = c === 'CLOSED' ? 'ok' : c === 'HALF_OPEN' ? 'warn' : c === 'OPEN' ? 'err' : 'mute'
    const calls2 = metrics.value?.providers?.[k] || {}
    const ok = Number(calls2.ok) || 0
    const fail = Number(calls2.fail) || 0
    const tot = ok + fail
    return {
      name: p[k].name,
      cls,
      text: t(`dashboard.circ_${String(p[k].circuit || '').toLowerCase() || ''}`),
      ratio: tot ? Math.max(6, Math.round((ok / tot) * 100)) : 6,
    }
  })
})

const routeRows = computed(() => {
  const m = models.value
  return routes.value.map((r: any) => {
    const chain: string[] = (m[r.id] || []).map((x: any) => (typeof x === 'string' ? x : x.id || x.name))
    return { name: r.id, short: chain.length ? t('models.chainCount', { n: chain.length }) : '—' }
  })
})

const callRows = computed(() => {
  const c = calls.value
  return Object.keys(c)
    .map((m) => {
      const ok = Number(c[m]?.ok) || 0
      const fail = Number(c[m]?.fail) || 0
      const tot = ok + fail
      const r = tot ? (ok / tot) * 100 : 100
      const p50 = latency.value[m]?.p50
      return {
        model: m,
        provider: textModels.value[m] || '—',
        calls: tot,
        latency: p50 != null ? Number(p50).toFixed(2) + 's' : '—',
        rate: r.toFixed(1) + '%',
        rateCls: r >= 99 ? 'ok' : r >= 90 ? 'warn' : 'err',
        _tot: tot,
      }
    })
    .sort((a, b) => b._tot - a._tot)
    .slice(0, 8)
})

const latOpt = computed<EChartsCoreOption>(() => {
  void resolved.value
  const c = chartTheme()
  const l = latency.value
  const ms = Object.keys(l).slice(0, 10)
  const p50 = ms.map((m) => l[m]?.p50 ?? 0)
  const p95 = ms.map((m) => l[m]?.p95 ?? 0)
  return {
    tooltip: { trigger: 'axis', valueFormatter: (v: any) => `${typeof v === 'number' ? v.toFixed(3) : v}s` },
    legend: legend(['P50', 'P95']),
    grid,
    graphic: emptyTip([p50, p95], t('dashboard.noData')),
    xAxis: categoryAxis(ms),
    yAxis: valueAxis('s'),
    series: [
      { name: 'P50', type: 'bar', data: p50, itemStyle: { color: c.a, borderRadius: [4, 4, 0, 0] } },
      { name: 'P95', type: 'bar', data: p95, itemStyle: { color: c.b, borderRadius: [4, 4, 0, 0] } },
    ],
  } as EChartsCoreOption
})

const provOpt = computed<EChartsCoreOption>(() => {
  void resolved.value
  const p = metrics.value.providers || {}
  const items = Object.keys(p).map((k) => ({ name: k, value: p[k]?.ok || 0 })).filter((x) => x.value > 0)
  return {
    tooltip: { trigger: 'item' },
    series: [{
      type: 'pie', radius: pieRadius, center: pieCenter, minAngle: 6, labelLine: pieLabelLine,
      data: pieData(items, t('dashboard.noData')), label: pieLabel(), itemStyle: { borderRadius: 4 },
    }],
  }
})

// 运行时间实时跳动：/health 是 5s 一拉，直接用返回值会「5 秒跳一格」。
const uptimeAt = ref(Date.now())
const nowTick = ref(Date.now())
const uptimeText = computed(() => {
  const u = health.value.uptime_seconds
  if (u == null) return '-'
  void nowTick.value
  const live = u + Math.max(0, (nowTick.value - uptimeAt.value) / 1000)
  const h = Math.floor(live / 3600), m = Math.floor((live % 3600) / 60), s = Math.floor(live % 60)
  return `${h}h ${m}m ${s}s`
})

async function load() {
  loading.value = true
  err.value = ''
  try {
    health.value = await api('/health')
    uptimeAt.value = Date.now()
    try { metrics.value = await api('/metrics') } catch { /* 指标可选 */ }
    try {
      const um = await api('/v1/models')
      const data = um.data || []
      routes.value = data.filter((x: any) => x.type === 'route')
      const map: Record<string, string> = {}
      for (const x of data) if (x.type !== 'route') map[x.id] = x.provider_name || ''
      textModels.value = map
      const list = data.filter((x: any) => x.type === 'route')
      const m: Record<string, any> = {}
      for (const r of list) m[r.id] = r.models || []
      models.value = m
    } catch { /* 模型列表可选 */ }
  } catch (e: any) {
    if (e && e.status === 401) { router.push('/login'); return }
    err.value = t('settings.loadErr') + ': ' + ((e && e.message) || String(e))
  } finally {
    loading.value = false
  }
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
.kpi .v { color: var(--text); }
.sep { height: 1px; background: var(--line-2); margin: 12px 0 10px; }
.about-card { margin-top: var(--gap); padding: 12px 16px; }
.about-line b { color: var(--text); }
</style>
