<template>
  <div class="page">
    <el-alert v-if="err" :title="err" type="error" show-icon :closable="false" class="mb" />

    <div class="kpis">
      <div v-for="c in cards" :key="c.k" class="kpi">
        <div class="k">{{ c.k }}</div>
        <div class="v">{{ c.v }}</div>
        <div class="f"><span class="muted-2">{{ c.note }}</span></div>
      </div>
    </div>

    <div class="grid2">
      <!-- 全部文本模型 + 一键测速（延迟逐行填充；测速按模型自己的协议线探测） -->
      <div class="card">
        <div class="card-h">
          <b>{{ t('models.allTextModels') }}</b>
          <span class="sub">{{ t('models.speedtestHint') }}</span>
          <span style="flex:1"></span>
          <el-button size="small" :loading="latTesting" @click="runSpeedtest()">
            {{ latTesting ? t('models.testingShort') : t('models.speedtest') }}
          </el-button>
        </div>

        <div class="lat-head">
          <span class="muted-2 hint">{{ t('models.allTextModels') }}</span>
          <span style="flex:1"></span>
          <span class="muted-2 hint">{{ t('models.latency') }}</span>
        </div>
        <div v-for="m in textModelRows" :key="m.id" class="st-item">
          <div class="st-row" :title="rowTitle(m)">
            <Icon v-if="m.vision" name="eye" :size="12" class="vision" />
            <!-- 第二十五轮：该模型的参数被上游固定（如 temperature 只能是 1），
                 挂锁标注 + 悬浮说明，省掉「为什么我传的参数不生效」这类困惑。 -->
            <Icon v-if="m.paramNote" name="lock" :size="12" class="plock" :title="m.paramNote" />
            <span class="code">{{ m.id }}</span>
            <!-- 支持的格式不在这里铺标签：这行还要放供应商/延迟/测速按钮，窄卡放不下第三颗
                 （悬停整行可见格式 + 本次测速走的线，见 rowTitle）。权威展示在下方表格的
                 「支持的格式」列。 -->
            <span class="muted-2 st-prov">{{ m.provider }}</span>
            <span style="flex:1"></span>
            <span class="pill st-lat" :class="latCls(m.id)">
              <span v-if="m.id === latCurrentId" class="st-spin" aria-hidden="true"></span>
              <span v-else>{{ latText(m.id) }}</span>
            </span>
            <button class="st-one" :disabled="latTesting || !!latCurrentId"
                    :title="t('models.testOne')" :aria-label="t('models.testOne')" @click="retestOne(m.id)">
              <Icon name="bolt" :size="13" />
            </button>
          </div>
          <div v-if="latErr(m.id)" class="st-err">
            <Icon name="ban" :size="13" class="st-err-ico" />
            <span class="st-err-txt">{{ latErr(m.id) }}</span>
          </div>
        </div>
        <div v-if="!textModelRows.length" class="hint">{{ t('common.empty') }}</div>
      </div>

      <!-- 供应商：状态取 /health 的熔断器，Key 是否配置取同一份数据 -->
      <div class="card">
        <div class="card-h">
          <b>{{ t('models.providerCard') }}</b>
          <span class="sub">{{ t('models.providerCount2', { n: providerRows.length }) }}</span>
        </div>
        <div v-for="p in providerRows" :key="p.name" class="prov">
          <div class="prov-top">
            <b>{{ p.name }}</b>
            <span class="pill" :class="p.cls">{{ p.text }}</span>
          </div>
          <div class="prov-meta">
            <span class="muted-2">{{ p.models }} {{ t('models.model') }}</span>
            <span class="pill" :class="p.keySet ? 'ok' : 'err'">
              {{ p.keySet ? t('models.keyOk') : t('models.keyMissing') }}
            </span>
          </div>
        </div>
        <div v-if="!providerRows.length" class="hint">{{ t('common.empty') }}</div>
      </div>
    </div>

    <div class="card" style="margin-top: var(--gap)">
      <div class="card-h">
        <b>{{ t('models.modelListTitle') }}</b>
        <span class="sub">{{ t('models.capability') }}</span>
        <div class="pager">
          <span class="pill mute">{{ t('models.textModelCount', { n: textModelRows.length }) }}</span>
        </div>
      </div>
      <el-table :data="modelRows" size="small" v-loading="loading">
        <el-table-column :label="t('models.id')" min-width="185">
          <template #default="{ row }"><span class="code">{{ row.id }}</span></template>
        </el-table-column>
        <el-table-column :label="t('models.type')" width="90">
          <template #default="{ row }"><span class="pill" :class="typeCls(row.type)">{{ t(typeKey(row.type)) }}</span></template>
        </el-table-column>
        <el-table-column :label="t('models.provider')" min-width="140">
          <template #default="{ row }">{{ row.provider_name || '—' }}</template>
        </el-table-column>
        <!-- 第二十七轮追加：支持的格式（三条协议线）——
             后端 /v1/models 里每个文本模型带 protocols 数组（chat/completions、messages、
             responses 的一个子集）。这里只做展示，但它有实际约束力：用不在列表里的格式
             调网关，会在入口被 400 挡下（网关不做跨格式转换，见 /v1/messages 的 400 文案）。
             图像/视频模型走专用端点，不涉及三条协议线，统一显示「不适用」。
             width 取 300：实测三颗标签（chat/completions=120px + messages=67px + responses=74px
             + 间距）并排一行需 ~273px，留足余量让多线模型那行也单行，不挤到换行。 -->
        <el-table-column width="300">
          <template #header>
            <el-tooltip :content="t('models.protocolHint')" placement="top">
              <span class="proto-th">{{ t('models.protocols') }}</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <span v-if="protocolsOf(row).length" class="proto-cell">
              <span v-for="p in protocolsOf(row)" :key="p" class="pill proto" :title="protoTitle(p)">{{ p }}</span>
            </span>
            <el-tooltip v-else :content="t('models.protocolNaHint')" placement="top">
              <span class="muted-2">{{ t('models.protocolNa') }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column :label="t('models.capability')" width="170">
          <template #default="{ row }">
            <span v-if="row.vision" class="pill ok cap">
              <Icon name="eye" :size="12" />{{ t('models.visionTag') }}
            </span>
            <span v-if="paramKeys(row).length" class="pill warn cap" :title="paramNote(row)">
              <Icon name="lock" :size="12" />{{ t('models.paramTag') }}
            </span>
            <span v-if="!row.vision && !paramKeys(row).length" class="muted-2">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('models.keySet')" width="90">
          <template #default="{ row }">
            <span class="pill" :class="row.key_set ? 'ok' : 'err'">
              {{ row.key_set ? t('common.yes') : t('common.no') }}
            </span>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { api } from '../api'
import Icon from '../components/Icon.vue'

const router = useRouter()
const { t } = useI18n()
const items = ref<any[]>([])
const health = ref<any>({})
const loading = ref(false)
const err = ref('')

const typeKeyMap: Record<string, string> = { text: 'models.text', image: 'models.image', video: 'models.video' }
const typeClsMap: Record<string, string> = { text: 'mute', image: 'warn', video: 'warn' }
function typeKey(ty: string) { return typeKeyMap[ty] || ty }
function typeCls(ty: string) { return typeClsMap[ty] || 'mute' }

// ── 支持的格式（第二十七轮）──
// 取值范围与后端 PROTOCOLS 一致：chat/completions（OpenAI 兼容线）、messages（Anthropic 线）、
// responses（OpenAI Responses 线）。**名字直接取各家标准端点名**，不另起中文别名 —— 配置、
// 日志、前端标签、上游文档四处同名，出问题时对得上号。
function protocolsOf(row: any): string[] {
  if (!row || row.type !== 'text') return []     // 图像/视频模型走专用端点，没有协议线概念
  return (row.protocols as string[]) || []
}
// 每个格式标签的悬浮说明：这条线对应哪个端点（点进说明文档也是这个对应关系）
function protoTitle(p: string): string { return `POST /v1/${p}` }

const latTesting = ref(false)
const latCurrentId = ref('')   // 当前正在测的模型 id：该行延迟位显示加载图标
// protocol 由后端返回：探针**按模型自己的协议线**挑一条（优先级 chat → messages → responses），
//   记下来才能在悬浮提示里说清「这次多少毫秒是走哪条线测的」——多线模型不写清楚会让人误解。
const latByModel = ref<Record<string, { ok: boolean; latency_ms: number | null; error: string; protocol: string }>>({})
async function runSpeedtest() {
  if (latTesting.value) return
  // 逐个测速：按列表顺序，测一个立刻显示那一行、再测下一个；当前测的行显示加载图标。
  //   串行天然不会把免费额度瞬间打满（并发时免费档供应商容易 429）。
  const ids = textModelRows.value.map((m) => m.id)
  latByModel.value = {}
  latTesting.value = true
  for (const id of ids) {
    latCurrentId.value = id
    let row: { ok: boolean; latency_ms: number | null; error: string; protocol: string }
    try {
      const j = await api('/v1/models/speedtest', { method: 'POST', body: JSON.stringify({ model: id }) })
      const r: any[] = (j && j.results) || []
      const hit = r[0]
      row = hit
        ? { ok: !!hit.ok, latency_ms: hit.latency_ms ?? null, error: hit.error || '', protocol: hit.protocol || '' }
        : { ok: false, latency_ms: null, error: t('models.speedtestFail'), protocol: '' }
    } catch (e: any) {
      // 单个模型出错不中断整轮，标记失败后继续下一个
      row = { ok: false, latency_ms: null, error: (e && e.message) || String(e), protocol: '' }
    }
    latByModel.value = { ...latByModel.value, [id]: row }   // 响应式替换 → 该行即时更新
  }
  latCurrentId.value = ''
  latTesting.value = false
  ElMessage.success(t('models.speedtestDone', { n: ids.length }))
}
// 单个模型重新测速（失败行旁的「重新测速」按钮）：只重测这一个，复用当前行加载图标
async function retestOne(id: string) {
  if (latTesting.value || latCurrentId.value) return   // 整轮测速中 / 已有单测在跑 → 忽略，避免打架
  latCurrentId.value = id
  try {
    const j = await api('/v1/models/speedtest', { method: 'POST', body: JSON.stringify({ model: id }) })
    const r: any[] = (j && j.results) || []
    const hit = r[0]
    const row = hit
      ? { ok: !!hit.ok, latency_ms: hit.latency_ms ?? null, error: hit.error || '', protocol: hit.protocol || '' }
      : { ok: false, latency_ms: null, error: t('models.speedtestFail'), protocol: '' }
    latByModel.value = { ...latByModel.value, [id]: row }
  } catch (e: any) {
    latByModel.value = { ...latByModel.value, [id]: { ok: false, latency_ms: null, error: (e && e.message) || String(e), protocol: '' } }
  } finally {
    latCurrentId.value = ''
  }
}
function latText(id: string): string {
  const row = latByModel.value[id]
  if (row) {
    if (row.ok) return row.latency_ms != null ? row.latency_ms + ' ms' : t('models.failed')
    return t('models.failed')   // 完整错误在下方 st-err 整行展示，这里只留短标记
  }
  return '—'   // 未轮到 / 空闲（当前正在测的行由模板的加载图标分支显示，不走这里）
}
function latErr(id: string): string {
  const row = latByModel.value[id]
  return row && !row.ok ? (row.error || '') : ''
}
function latCls(id: string): string {
  const row = latByModel.value[id]
  return row ? (row.ok ? 'ok' : 'err') : 'mute'
}

// 第二十七轮: 路由别名条目（type === 'route'）已从 /v1/models 消失 → 这里只剩真实模型
const models = computed(() => items.value)

const providerOf = computed(() => {
  const m: Record<string, string> = {}
  for (const x of models.value) m[x.id] = x.provider_name || '—'
  return m
})
const visionOf = computed(() => {
  const m: Record<string, boolean> = {}
  for (const x of models.value) m[x.id] = !!x.vision
  return m
})

const textModelRows = computed(() =>
  models.value.filter((x) => x.type === 'text').map((m) => ({
    id: m.id,
    provider: m.provider_name || '—',
    vision: !!m.vision,
    // 模型级强制参数（第二十五轮）：被上游固定住的采样参数，用挂锁标注出来。
    paramNote: paramKeys(m).length ? paramNote(m) : '',
    // 支持的格式（第二十七轮）：悬浮这一行就能看到「能用哪几条线 + 本次测速走的哪条」
    formats: protocolsOf(m),
  })),
)

// 测速行的悬浮提示：先说这个模型支持哪些格式，再说这次测的是哪条线
//   （多线模型不标注的话，"12 ms"到底测的是哪条线是看不出来的）。
function rowTitle(m: any): string {
  const lines = (m.formats || []).join(' / ') || t('models.protocolNa')
  const p = latByModel.value[m.id]?.protocol
  return p
    ? `${t('models.protocolHintRow', { lines })}· ${t('models.probedLine', { p })}`
    : t('models.protocolHintRow', { lines })
}

// ── 模型级强制参数（第二十五轮）──
// 后端 /v1/models 在 params 里给出「该模型发往上游前会被强制覆盖的参数」
// （例：kimi-k2.7-code 只接受 temperature=1）。这里只做展示，不提供编辑入口。
function paramKeys(m: any): string[] {
  return Object.keys((m && m.params) || {})
}
function paramNote(m: any): string {
  const ps = (m && m.params) || {}
  const txt = paramKeys(m).map((k) => `${k}=${ps[k]}`).join(', ')
  return t('models.paramLocked', { p: txt })
}

const keySet = computed(() => {
  const ks: Record<string, boolean> = {}
  for (const p of Object.values(health.value.providers || {}) as any) ks[p.name] = !!p.api_key_set
  return ks
})
const modelRows = computed(() => models.value.map((m) => ({ ...m, key_set: keySet.value[m.provider_name] })))

const providerRows = computed(() => {
  const p = health.value.providers || {}
  return Object.keys(p).map((k) => {
    const c = String(p[k].circuit || '').toUpperCase()
    return {
      name: p[k].name,
      models: p[k].models,
      keySet: !!p[k].api_key_set,
      cls: c === 'CLOSED' ? 'ok' : c === 'HALF_OPEN' ? 'warn' : c === 'OPEN' ? 'err' : 'mute',
      text: t(`dashboard.circ_${String(p[k].circuit || '').toLowerCase() || ''}`),
    }
  })
})

const cards = computed(() => [
  { k: t('models.model'), v: models.value.length, note: t('models.providerCount2', { n: providerRows.value.length }) },
  { k: t('models.textModels'), v: models.value.filter((m) => m.type === 'text').length, note: t('models.visionTag') + ' · ' + models.value.filter((m) => m.vision).length },
  { k: t('models.imageModels'), v: models.value.filter((m) => m.type === 'image').length, note: t('images.title') },
])

onMounted(async () => {
  loading.value = true
  try {
    const j = await api('/v1/models')
    items.value = j.data || []
    try { health.value = await api('/health') } catch { /* 可选 */ }
  } catch (e: any) {
    if (e && e.status === 401) { router.push('/login'); return }
    err.value = t('models.loadErr') + ': ' + ((e && e.message) || String(e))
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.chain { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.chain-item { display: inline-flex; align-items: center; gap: 6px; }
.arrow { color: var(--muted-2); }
.vision { color: var(--primary); }
.plock { color: var(--warn); }
/* 支持的格式：三条线是并列关系（没有谁更优），统一用主题色弱底，
   不用 ok/warn/err —— 否则「messages」看起来像警告、「responses」像被禁用。 */
.proto {
  color: var(--primary); background: var(--primary-soft);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;   /* 与全局 .code 同一套 */
  font-size: 11px; padding: 2px 7px;
}
.proto-cell { display: inline-flex; align-items: center; gap: 5px; flex-wrap: wrap; }
.proto-th { border-bottom: 1px dotted var(--muted-2); cursor: help; }
.prov { padding: 10px 0; border-bottom: 1px solid var(--line-2); }
.prov:last-child { border-bottom: 0; }
.prov-top { display: flex; align-items: center; gap: 8px; }
.prov-top b { font-size: 13px; }
.prov-meta { display: flex; align-items: center; gap: 8px; margin-top: 6px; font-size: 11.5px; }
.cap { display: inline-flex; align-items: center; gap: 4px; }
.st { margin-top: 8px; border-top: 1px dashed var(--line-2); padding-top: 6px; }
.st-item { padding: 4px 0; }
.st-item + .st-item { border-top: 1px dashed var(--line); }
.st-row { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.st-err {
  display: flex; align-items: flex-start; gap: 6px;
  margin: 4px 2px 2px; padding: 6px 8px;
  border-radius: 8px;
  background: var(--err-soft);
  color: var(--err);
  font-size: 11.5px; line-height: 1.55;
  overflow-wrap: anywhere;   /* 长错误（JSON / 无空格串）也能自动换行不溢出 */
  word-break: break-word;
}
.st-err-ico { flex: none; margin-top: 1px; color: var(--err); }
.st-err-txt { flex: 1; min-width: 0; }
/* 每个模型行右侧的「单独测速」图标按钮 */
.st-one {
  flex: none; display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; padding: 0; margin-left: 2px;
  border: 1px solid var(--line); background: transparent; color: var(--muted);
  border-radius: 8px; cursor: pointer; transition: color .15s, border-color .15s, background .15s;
}
.st-one:hover:not(:disabled) { color: var(--primary); border-color: var(--primary); background: var(--primary-soft); }
.st-one:disabled { opacity: 0.45; cursor: not-allowed; }
/* 当前正在测的模型行：延迟位显示一个旋转加载图标 */
.st-spin {
  display: inline-block; width: 12px; height: 12px; vertical-align: middle;
  border: 2px solid var(--line); border-top-color: var(--primary);
  border-radius: 50%; animation: st-spin 0.7s linear infinite;
}
@keyframes st-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .st-spin { animation-duration: 1.6s; } }
.st-title { font-weight: 600; color: var(--text); }
.st-prov { flex: 1; }
.st-lat { min-width: 88px; text-align: center; }
.st-loading { font-style: italic; }
.lat-head { display: flex; align-items: center; gap: 8px; padding: 2px 0 6px; }
</style>
