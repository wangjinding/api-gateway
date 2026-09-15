<template>
  <div class="page">
    <!-- 筛选行：类型胶囊 + 原有筛选控件 + 刷新/导出 -->
    <div class="filters">
      <!-- 第十九轮：类型胶囊与「失败」互斥（原来两个维度叠加，「失败」亮着时点类型只剩空列表，
           看起来像筛选坏了）；并给每个胶囊带上条数，点完立刻能看到生效与是否有该类型记录。 -->
      <span class="chip-sel" :class="{ on: kind === '' && result !== 'fail' }" @click="pickKind('')">
        {{ t('logs.all') }}<i class="cn">{{ entries.length }}</i>
      </span>
      <span class="chip-sel" :class="{ on: kind === 'chat' }" @click="pickKind('chat')">
        {{ t('logs.chat') }}<i class="cn">{{ kindCounts.chat }}</i>
      </span>
      <span class="chip-sel" :class="{ on: kind === 'image' }" @click="pickKind('image')">
        {{ t('logs.image') }}<i class="cn">{{ kindCounts.image }}</i>
      </span>
      <span class="chip-sel" :class="{ on: kind === 'video' }" @click="pickKind('video')">
        {{ t('logs.video') }}<i class="cn">{{ kindCounts.video }}</i>
      </span>
      <span class="chip-sel" :class="{ on: result === 'fail' }" @click="pickKind('fail')">
        {{ t('logs.fail') }}<i class="cn">{{ kindCounts.fail }}</i>
      </span>

      <el-select v-model="method" :placeholder="t('logs.method')" clearable size="small" class="sel-method">
        <el-option v-for="m in ['GET', 'POST', 'OPTIONS', 'DELETE']" :key="m" :value="m" :label="m" />
      </el-select>
      <el-select v-model="result" :placeholder="t('logs.result')" clearable size="small" class="sel-result">
        <el-option value="ok" :label="t('logs.success')" />
        <el-option value="fail" :label="t('logs.fail')" />
      </el-select>
      <el-input v-model="kw" size="small" :placeholder="t('logs.searchPlaceholder')" clearable class="kw-in" @keyup.enter="applyFilter">
        <template #append><el-button @click="applyFilter">{{ t('common.search') }}</el-button></template>
      </el-input>
      <el-switch v-model="auto" size="small" :active-text="t('logs.autoRefresh')" />

      <span class="spacer" />
      <el-button size="small" text @click="clear">{{ t('logs.clearFilter') }}</el-button>
      <el-button size="small" :disabled="loading" @click="load()">
        <Icon name="refresh" :size="13" />
        <span>{{ t('common.refresh') }}</span>
      </el-button>
      <el-button size="small" :disabled="!filtered.length" @click="exportLogs">
        <Icon name="dl" :size="13" />
        <span>{{ t('common.export') }}</span>
      </el-button>
    </div>

    <div class="card">
      <div class="card-h">
        <b>{{ t('nav.menulogs') }}</b>
        <span class="sub">{{ t('logs.count', { n: filtered.length }) }}</span>
        <div class="pager">
          <span class="pill" :class="rateCls">{{ t('logs.success') }} {{ okRate }}%</span>
        </div>
      </div>

      <div class="log-list" v-loading="loading">
        <div v-if="!filtered.length" class="hint">
          <span>{{ entries.length ? t('logs.emptyFiltered', { n: entries.length }) : t('logs.empty') }}</span>
          <el-button v-if="entries.length" size="small" text @click="clear">{{ t('logs.clearFilter') }}</el-button>
        </div>

        <div v-for="row in filtered" :key="rowKey(row)">
          <div class="log-row" @click="toggle(row)">
            <span class="ts">{{ fmtTime(row.ts) }}</span>
            <span class="badge m">{{ kindLabel(row) }}</span>
            <span class="badge" :class="statusCls(row)">{{ statusText(row) }}</span>
            <span class="code path">{{ row.method }} {{ row.path }}</span>
            <span class="badge m" v-if="row.stream">{{ t('logs.streaming') }}</span>
            <span class="nowrap model">{{ row.served || row.model || '—' }}<span v-if="row.provider" class="muted-2"> @ {{ row.provider }}</span></span>
            <span class="muted note" :title="noteFull(row)">{{ noteText(row) }}</span>
            <Icon :name="isOpen(row) ? 'chevD' : 'chev'" :size="14" />
          </div>

          <div v-if="isOpen(row)" class="log-detail">
            <p class="kv">
              <b>{{ t('logs.rid') }}</b> <span class="code">{{ row.rid || '—' }}</span>
              <b>{{ t('logs.caller') }}</b> <span class="code">{{ row.caller || '—' }}</span>
              <b>{{ t('logs.reqModel') }}</b> <span class="code">{{ row.model || '—' }}</span>
              <b>{{ t('logs.served') }}</b> <span class="code">{{ row.served || '—' }}</span>
              <b>{{ t('logs.provider') }}</b> <span class="code">{{ row.provider || '—' }}</span>
              <b>{{ t('logs.streaming') }}</b> <span class="code">{{ row.stream ? 'SSE' : t('common.no') }}</span>
            </p>
            <div class="detail-block"><label>{{ t('logs.request') }}</label><pre>{{ row.body || '—' }}</pre></div>
            <div class="detail-block"><label>{{ t('logs.response') }}</label><pre>{{ row.response || '—' }}</pre></div>
            <div class="detail-block" v-if="row.polls && row.polls.length">
              <label>{{ t('logs.polls', { n: row.polls.length }) }}</label>
              <el-timeline class="poll-timeline">
                <el-timeline-item v-for="(p, i) in row.polls" :key="i"
                  :type="p.ok === false ? 'danger' : p.status === 'completed' || p.status === 'success' ? 'success' : p.status === 'failed' ? 'danger' : 'info'"
                  :timestamp="fmtTime(p.ts)" placement="top" size="small">
                  <div class="poll-line">
                    <span class="code">{{ p.status || '—' }}</span>
                    <span v-if="p.progress != null" class="sub"> · {{ p.progress }}%</span>
                    <span v-if="p.error" class="err-text"> · {{ short(p.error) }}</span>
                  </div>
                  <pre v-if="p.raw" class="poll-raw">{{ p.raw }}</pre>
                </el-timeline-item>
              </el-timeline>
            </div>
            <div class="detail-block" v-if="!row.ok"><label>{{ t('logs.error') }}</label><pre class="err-pre">{{ row.error || '—' }}</pre></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { api } from '../api'
import Icon from '../components/Icon.vue'
import { stamp } from '../lib/download'

const router = useRouter()
const { t } = useI18n()
const entries = ref<any[]>([])
const loading = ref(false)
const auto = ref(true)
const method = ref('')
const result = ref('')
const kw = ref('')
// 第十九轮真凶：这里原来是普通 `let appliedKw = ''`，不是 ref —— 它不参与响应式追踪，
// 改完之后 `filtered` 这个 computed 不会重算，只能等下一次数据变化（自动刷新 5s 一轮）
// 才顺带把关键词应用上。表现就是「输了关键词点搜索，列表纹丝不动 / 过几秒自己又变了」。
// 类型胶囊用的是 ref（kind），所以只有关键词这一路看着像坏了。
const appliedKw = ref('')
// 已展开的行 key：自动刷新会整体替换数据数组。改成自绘行之后展开态直接由这个 key 集合推导，
// 刷新后（只要这行还在）详情不会每 5s 被收回去。
const expandedKeys = ref<string[]>([])
const expandedSet = computed(() => new Set(expandedKeys.value))
const rowKey = (row: any): string => row.rid || `${row.method}:${row.path}:${row.ts}`
function isOpen(row: any): boolean {
  return expandedSet.value.has(rowKey(row))
}
function toggle(row: any) {
  const k = rowKey(row)
  expandedKeys.value = expandedKeys.value.includes(k)
    ? expandedKeys.value.filter((x) => x !== k)
    : [...expandedKeys.value, k]
}

// 视频快照是否已经有结论（有结论就不再主动追轮询）
const pollDone = (pl: any) => /completed|success|succeeded|failed|error/i.test(String(pl?.status))

async function load() {
  loading.value = true
  try {
    // 先对上一条数据里「仍展开、且没生成完」的视频行主动轮询一次（幂等 GET）：
    // 让正在看日志详情的老板也能实时看到轮询进度 —— 否则只有开着视频页才有快照。
    // 先轮询再取 /logs，本次刷新立刻就能看到新快照，不会慢一拍。
    const keys = expandedSet.value
    for (const e of entries.value) {
      if (!e.video_id || !keys.has(rowKey(e))) continue
      const pl = e.polls && e.polls.length ? e.polls[e.polls.length - 1] : null
      if (pl && pollDone(pl)) continue
      try {
        await api(`/v1/videos/${encodeURIComponent(e.video_id)}?model=${encodeURIComponent(e.served || e.model || '')}`)
      } catch { /* 轮询失败不打断刷新；后端会记为该次的失败快照 */ }
    }
    const j = await api('/logs?n=200')
    entries.value = j.entries || []
    // 只保留仍存在的已展开行：行被顶出窗口/被删则对应 key 一并清掉
    const alive = new Set(entries.value.map(rowKey))
    expandedKeys.value = expandedKeys.value.filter((k) => alive.has(k))
  } catch (e: any) {
    if (e && e.status === 401) { router.push('/login'); return }
  } finally { loading.value = false }
}

// ── 类型筛选（全部 / 对话 / 图像 / 视频 / 失败）──
// 「失败」直接复用原有的 result 筛选，不另立一份状态；其余按日志路径归类，纯前端过滤。
type Kind = '' | 'chat' | 'image' | 'video'
const kind = ref<Kind>('')
// 第十九轮：优先用后端给的 row.kind（权威），老条目没这个字段才退回按路径猜。
// 之前只看路径字符串，一旦以后端点改名/换前缀，筛选就会静默失效。
function kindOf(row: any): Kind {
  const k = String(row.kind || '')
  if (k === 'chat' || k === 'image' || k === 'video') return k
  if (k === 'media') return ''          // 媒体代拉（下载）：不属于三类生成调用，只在「全部」里出现
  const p = String(row.path || '')
  if (p.includes('/images')) return 'image'
  if (p.includes('/videos')) return 'video'
  if (p.includes('/chat')) return 'chat'
  return ''
}
// 每个胶囊上的条数：直接告诉老板「这一类有几条」，筛完变空时也就不会再以为筛选没生效
const kindCounts = computed(() => {
  const c: Record<string, number> = { chat: 0, image: 0, video: 0, fail: 0 }
  for (const e of entries.value) {
    const k = kindOf(e)
    if (k) c[k] += 1
    if (!e.ok) c.fail += 1
  }
  return c
})
function pickKind(k: '' | Kind | 'fail') {
  if (k === '') { kind.value = ''; result.value = ''; return }
  if (k === 'fail') {
    // 互斥：点「失败」就清掉类型，点类型就清掉「失败」（否则 类型=视频 + 结果=失败
    // 两个胶囊同时亮着、列表却是空的，看着就像筛选坏了）。要叠加筛选仍可用「结果」下拉。
    result.value = result.value === 'fail' ? '' : 'fail'
    if (result.value) kind.value = ''
    return
  }
  kind.value = kind.value === k ? '' : k
  if (kind.value) result.value = ''
}
function kindLabel(row: any): string {
  const k = kindOf(row)
  if (k === 'chat') return t('logs.chat')
  if (k === 'image') return t('logs.image')
  if (k === 'video') return t('logs.video')
  if (row.kind === 'media') return t('logs.media')   // 下载：比裸显示 GET 更说明问题
  return row.method || '—'
}
// 视频还没出结果 → 用 warn 色提示「生成中」
function videoBusy(row: any): boolean {
  if (!row.video_id) return false
  const pl = row.polls && row.polls.length ? row.polls[row.polls.length - 1] : null
  return !pl || !pollDone(pl)
}
function statusCls(row: any): string {
  if (!row.ok) return 'r'
  return videoBusy(row) ? 'o' : 'g'
}
function statusText(row: any): string {
  if (!row.ok) return t('common.fail')
  return videoBusy(row) ? t('videos.doingText') : t('common.ok')
}

const filtered = computed(() => {
  let list = entries.value
  if (method.value) list = list.filter((e) => e.method === method.value)
  if (result.value) list = list.filter((e) => e.ok === (result.value === 'ok'))
  if (kind.value) list = list.filter((e) => kindOf(e) === kind.value)
  if (appliedKw.value) {
    const k = appliedKw.value.toLowerCase()
    list = list.filter((e) => [e.model, e.served, e.provider, e.path, e.body, e.response, e.error]
      .some((v) => v && String(v).toLowerCase().includes(k)))
  }
  return list
})
function applyFilter() { appliedKw.value = (kw.value || '').trim() }
// 第十九轮：关键词改成「边打边筛」。原来只有点「搜索」或回车才赋值，而且那个变量还不是响应式的，
// 输完看着列表没变，很容易和这次报的「筛选不生效」混在一起；行数最多 200 条，实时过滤开销可忽略。
// 搜索按钮和回车保留（点一下等于强制重算，对习惯动作无害）。
watch(kw, () => { appliedKw.value = (kw.value || '').trim() })
function clear() { method.value = ''; result.value = ''; kw.value = ''; appliedKw.value = ''; kind.value = '' }

const okRate = computed(() => {
  const n = filtered.value.length
  if (!n) return '0.0'
  const ok = filtered.value.filter((e) => e.ok).length
  return ((ok / n) * 100).toFixed(1)
})
const rateCls = computed(() => {
  const n = filtered.value.length
  if (!n) return 'mute'
  const r = Number(okRate.value)
  return r >= 99 ? 'ok' : r >= 90 ? 'warn' : 'err'
})

function fmtTime(ts: number) {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  const pad = (x: number) => String(x).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

const short = (x: any, n = 120) => {
  const t = String(x ?? '')
  return t.length > n ? t.slice(0, n) + '…' : t
}
function noteFull(row: any): string {
  return String((row.ok ? row.response : row.error || row.response) || row.body || '')
}
function noteText(row: any): string {
  const s = noteFull(row)
  return s ? short(s) : '—'
}

/** 日志导出：把当前筛选结果存成 JSON（纯前端，不新增接口）。 */
function exportLogs() {
  const body = JSON.stringify(filtered.value, null, 2)
  const href = URL.createObjectURL(new Blob([body], { type: 'application/json;charset=utf-8' }))
  const a = document.createElement('a')
  a.href = href
  a.download = `logs-${stamp()}.json`
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(href), 10_000)
}

let timer: any = null
onMounted(() => { load(); timer = setInterval(() => { if (auto.value) load() }, 5000) })
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.filters .spacer { flex: 1; }
.sel-method { width: 118px; }
.sel-result { width: 108px; }
/* 搜索框：桌面 280px，窄屏靠 max-width 收到工具条宽度（父级是 flex 容器，100% 有效） */
.kw-in { width: 280px; max-width: 100%; }

.log-list { max-height: 68vh; overflow-y: auto; }
.log-row { cursor: pointer; }
.log-row:hover { background: var(--surface-2); }
.log-row .path { flex: none; color: var(--muted); }
.log-row .model { flex: none; max-width: 220px; overflow: hidden; text-overflow: ellipsis; }
.log-row .note { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.log-row :deep(.gw-icon) { color: var(--muted-2); flex: none; }

.kv { margin: 0 0 8px; display: flex; gap: 10px; flex-wrap: wrap; }
.detail-block { margin-bottom: 8px; }
.detail-block label { display: block; color: var(--muted-2); font-size: 11px; margin-bottom: 2px; }
.detail-block pre {
  margin: 0; padding: 8px 10px; background: var(--surface); border: 1px solid var(--line);
  border-radius: 6px; white-space: pre-wrap; word-break: break-all;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px;
  max-height: 240px; overflow: auto; color: var(--text);
}
.err-pre { color: var(--err); }
.poll-timeline { padding: 4px 2px; max-height: 260px; overflow: auto; }
.poll-line { font-size: 12px; }
.poll-raw {
  margin-top: 2px; font-size: 11px; max-height: 90px; overflow: auto;
  background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px;
  white-space: pre-wrap; word-break: break-all;
}
.sub { font-size: 11.5px; color: var(--muted-2); }
.err-text { color: var(--err); font-size: 12px; }

/* 类型胶囊上的条数：小一号、淡一档；胶囊选中时跟着主色走（.on 已给 font-weight） */
.chip-sel .cn {
  font-style: normal; font-size: 11px; font-variant-numeric: tabular-nums;
  padding: 0 5px; border-radius: 6px; background: var(--chip); color: var(--muted-2);
}
.chip-sel.on .cn { background: var(--primary); color: #fff; }
/* 筛选后为空：给出「原始有多少条」并直接给一个清空筛选的出口 */
.hint { display: flex; align-items: center; justify-content: center; gap: 8px; flex-wrap: wrap; }

@media (max-width: 768px) {
  .filters .kw-in { width: 100%; }
  .log-row { flex-wrap: wrap; }
  .log-row .path { order: 9; width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .log-row .note { white-space: normal; }
}
</style>
