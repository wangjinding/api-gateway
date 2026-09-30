<template>
  <div class="page">
    <el-alert v-if="err" :title="err" type="error" show-icon :closable="false" class="mb" />

    <div class="row-h">
      <div>
        <b style="font-size:15px">{{ t('agents.title') }}</b>
        <span class="sub-hint">{{ t('agents.sub') }}</span>
      </div>
      <span style="margin-left:auto" />
      <el-checkbox v-if="hasRetired" v-model="showRetired" size="small" @change="load">
        {{ t('agents.showRetired') }}
      </el-checkbox>
      <el-button type="primary" size="small" :disabled="atLimit" :title="atLimit ? t('agents.limitHint', { n: data.max_agents }) : ''" @click="openCreate">
        <Icon name="plus" :size="14" />{{ t('agents.new') }}
      </el-button>
    </div>

    <!-- KPI -->
    <div class="kpis ag-kpis">
      <div class="kpi">
        <div class="k">{{ t('agents.title') }}</div>
        <div class="v">{{ activeCount }}</div>
        <div class="f"><span class="muted-2">{{ t('agents.limitHint', { n: data.max_agents }) }}</span></div>
      </div>
      <div class="kpi">
        <div class="k">{{ t('agents.cfgAccess') }}</div>
        <div class="v">{{ data.engine.ready ? '1' : '0' }}</div>
        <div class="f"><span class="muted-2">{{ t('agents.scopeAdmin') }}</span></div>
      </div>
      <div class="kpi">
        <div class="k">{{ t('agents.tabRuns') }}</div>
        <div class="v">{{ totalRuns }}</div>
        <div class="f"><span class="muted-2">{{ t('agents.runsLast7') }}</span></div>
      </div>
      <div class="kpi">
        <div class="k">{{ t('agents.tokens') }}</div>
        <div class="v">{{ totalTokensText }}</div>
        <div class="f"><span class="muted-2">{{ t('agents.runsDetailSub') }}</span></div>
      </div>
    </div>

    <div class="ag-layout">
      <!-- 左：列表 -->
      <div class="card" style="padding:12px">
        <div class="card-h ag-list-h">
          <b>{{ t('agents.title') }}</b>
          <span class="sub">{{ t('agents.sharedNote') }}</span>
        </div>
        <div class="ag-list">
          <div
            v-for="a in data.agents"
            :key="a.slug"
            class="ag"
            :class="{ on: a.slug === current }"
            @click="pick(a.slug)"
          >
            <div class="ag-ico"><Icon name="bot" :size="17" /></div>
            <div class="ag-main">
              <div class="ag-nm">{{ a.display_name }}</div>
              <div class="ag-ds">{{ a.description || '—' }}</div>
              <div class="ag-tags">
                <span class="pill mute">{{ t('agents.engineBadge') }}</span>
                <span v-if="a.engine_model" class="ag-md">{{ a.engine_model }}</span>
              </div>
            </div>
            <div class="ag-right">
              <span class="pill" :class="a.status === 'retired' ? 'arch' : (a.status === 'draft' ? 'mute' : 'ok')">
                {{ statusText(a.status) }}
              </span>
            </div>
          </div>
          <div v-if="!data.agents.length" class="hint">{{ t('agents.empty') }}</div>
          <div v-else class="ag-list-foot">{{ t('agents.listFootHint') }}</div>
        </div>
      </div>

      <!-- 右：详情 -->
      <div v-if="!cur" class="card ag-empty">
        <div class="ae-ico"><Icon name="bot" :size="34" /></div>
        <div class="ae-t">{{ t('agents.emptyPick') }}</div>
        <div class="ae-s">{{ t('agents.emptyHint') }}</div>
        <div class="ae-btn" @click="openCreate"><Icon name="plus" :size="14" />{{ t('agents.newAgent') }}</div>
      </div>

      <div v-else class="ag-detail">
        <!-- 顶部身份条 + Tab（合一张卡，避免卡片套卡片） -->
        <div class="card ag-head">
        <div class="row-h">
          <div class="ag-top">
            <div class="ag-face"><Icon name="bot" :size="16" /></div>
            <b>{{ cur.display_name }}</b>
            <span class="pill" :class="cur.status === 'retired' ? 'arch' : 'ok'">{{ statusText(cur.status) }}</span>
            <span v-if="cur.engine_ready" class="pill ok">{{ t('agents.engineReady') }}</span>
            <span v-else class="pill err">{{ t('agents.engineDown') }}</span>
          </div>
          <span style="margin-left:auto" />
          <el-button v-if="cur.status !== 'retired'" size="small" @click="doRetire">{{ t('agents.archive') }}</el-button>
          <el-button v-else size="small" type="primary" @click="doRestore">{{ t('agents.restore') }}</el-button>
        </div>

        <div class="ag-tabs">
          <span :class="{ on: tab === 'chat' }" @click="tab = 'chat'">{{ t('agents.tabChat') }}</span>
          <span :class="{ on: tab === 'config' }" @click="tab = 'config'">{{ t('agents.tabConfig') }}</span>
          <span :class="{ on: tab === 'runs' }" @click="tab = 'runs'; loadRuns()">{{ t('agents.tabRuns') }}</span>
        </div>
        </div>

        <!-- ① 对话 -->
        <div v-if="tab === 'chat'" class="chat-layout">
          <aside class="chat-side">
            <div class="side-in">
              <el-button type="primary" class="new-conv" :disabled="streaming" @click="newChat">
                <Icon name="plus" :size="14" /><span>{{ t('agents.newChat') }}</span>
              </el-button>
              <div class="conv-list">
                <div
                  v-for="c in sessions" :key="c.id" class="conv" :class="{ on: c.id === sessionId }"
                  @click="switchSession(c.id)"
                >{{ c.title }}
                  <small>{{ c.time }} · {{ c.n }} 条</small>
                </div>
                <div v-if="!sessions.length" class="hint">{{ t('common.empty') }}</div>
              </div>
              <div class="sep" />
              <div class="card-h" style="margin-bottom:var(--gap-xs)"><b style="font-size:12.5px">{{ t('agents.thisChat') }}</b></div>
              <div class="pv" style="gap:8px;font-size:12px">
                <div class="pv-i"><span class="nm">Tokens</span><span class="st">{{ lastUsage ? (lastUsage.prompt + lastUsage.completion) : 0 }}</span></div>
                <div class="pv-i"><span class="nm">{{ t('agents.toolsUsed') }}</span><span class="st">{{ stepsDone }}</span></div>
                <div class="pv-i"><span class="nm">{{ t('agents.elapsed') }}</span><span class="st">{{ elapsedText }}</span></div>
              </div>
              <div class="ops-row">
                <el-button v-if="streaming" size="small" class="op" @click="stopLocal">{{ t('agents.stop') }}</el-button>
              </div>
            </div>
          </aside>

          <section class="chat-main">
            <!-- 风险提示（决策 ⑪ 要求常驻） -->
            <div class="risk-bar"><Icon name="bell" :size="12" />{{ t('agents.riskNote') }}</div>

            <div class="chat-card">
              <div class="card-h">
                <b>{{ curSessionTitle }}</b>
                <span class="sub">{{ messages.length }} 条</span>
                <div class="pager">
                  <el-button size="small" :disabled="streaming || !messages.length" @click="copyAll">
                    <Icon name="copy" :size="13" /><span>{{ t('chat.copyAll') }}</span>
                  </el-button>
                </div>
              </div>

              <div ref="scrollEl" class="msgs">
                <div v-if="!messages.length" class="empty">
                  <Icon name="bot" :size="34" />
                  <div class="empty-t">{{ t('agents.emptyHint') }}</div>
                </div>

                <div v-for="m in messages" :key="m.id" class="msg" :class="m.role === 'user' ? 'me' : 'ai'">
                  <!-- ★ 工具执行轨迹 -->
                  <div v-if="m.role === 'assistant' && m.steps && m.steps.length" class="trace">
                    <div class="trace-h">
                      <Icon name="bolt" :size="12" />
                      {{ t('agents.execSteps', { n: m.steps.length }) }}
                      <span class="trace-caret" @click="m.open = !m.open">{{ m.open ? '▴' : '▾' }}</span>
                    </div>
                    <template v-if="m.open">
                      <div v-for="st in m.steps" :key="st.id" class="step" :class="st.status">
                        <Icon :name="st.status === 'done' ? 'check' : 'refresh'" :size="13" />
                        <span class="s-nm">{{ st.name || st.id }}</span>
                        <span class="s-dt">{{ stepDetail(st) }}</span>
                        <span class="s-ms">{{ st.ms != null ? st.ms + 'ms' : '' }}</span>
                      </div>
                    </template>
                  </div>

                  <div v-if="m.images && m.images.length" class="msg-imgs">
                    <img v-for="(u, i) in m.images" :key="i" :src="u" alt="" />
                  </div>

                  <div v-if="m.text" v-html="render(m.text)" />
                  <div v-else-if="streaming && m.role === 'assistant'" class="typing">…</div>

                  <div class="meta">
                    <template v-if="m.role === 'assistant' && m.meta">{{ m.meta }}</template>
                  </div>
                </div>
              </div>

              <div class="composer">
                <el-input
                  v-model="input"
                  type="textarea"
                  :rows="2"
                  resize="none"
                  :placeholder="t('agents.inputPlaceholder')"
                  @keydown.enter.exact.prevent="send"
                />
                <div v-if="pendingImgs.length" class="pending">
                  <div v-for="(im, i) in pendingImgs" :key="i" class="pend-i">
                    <img :src="im.thumb || im.uri" alt="" />
                    <span class="pend-x" @click="removeImg(i)">×</span>
                  </div>
                  <span class="pend-hint">{{ t('agents.imgCount', { n: pendingImgs.length, max: MAX_IMGS }) }}</span>
                </div>
                <div class="bar">
                  <input ref="fileInput" type="file" accept="image/*" multiple hidden @change="onFiles" />
                  <button class="clip-btn" :disabled="streaming" :title="t('agents.attach')" @click="fileInput?.click()">
                    <Icon name="clip" :size="15" />
                  </button>
                  <span class="tok">{{ cur.engine_model || '—' }}</span>
                  <span style="flex:1" />
                  <el-button v-if="!streaming" type="primary" size="small"
                             :disabled="!input.trim() && !pendingImgs.length" @click="send">
                    <Icon name="send" :size="13" />{{ t('chat.send') }}
                  </el-button>
                  <el-button v-else size="small" class="stop-btn" @click="stopLocal">
                    <Icon name="ban" :size="13" />{{ t('agents.stop') }}
                  </el-button>
                </div>
              </div>
            </div>
          </section>
        </div>

        <!-- ② 配置 -->
        <div v-if="tab === 'config'" class="cfg-grid">
          <div class="card" style="padding:14px">
            <div class="card-h"><b>{{ t('agents.cfgBasic') }}</b><span class="sub">{{ t('agents.cfgBasicSub') }}</span></div>
            <div class="frow">
              <div class="f-lab">{{ t('agents.cfgName') }}</div>
              <el-input v-model="form.display_name" size="small" />
            </div>
            <div class="frow">
              <div class="f-lab">{{ t('agents.cfgSlug') }}</div>
              <div class="f-val code">{{ cur.slug }}</div>
            </div>
            <div class="f-hint">{{ t('agents.cfgSlugHint') }}</div>
            <div class="frow">
              <div class="f-lab">{{ t('agents.cfgDesc') }}</div>
              <el-input v-model="form.description" size="small" />
            </div>
            <div class="row-actions">
              <el-button type="primary" size="small" :disabled="!dirty" @click="saveCfg">{{ t('agents.save') }}</el-button>
              <el-button size="small" :loading="testing" @click="doTest">
                {{ testing ? t('agents.testing') : t('agents.test') }}
              </el-button>
              <span v-if="testResult" class="test-res" :class="{ bad: !testResult.ok }">
                {{ testResult.ok ? t('agents.testOk', { ms: testResult.ms }) : t('agents.testFail') + '：' + testResult.detail }}
              </span>
            </div>
          </div>

          <div class="card" style="padding:14px">
            <div class="card-h"><b>{{ t('agents.cfgModel') }}</b><span class="sub">{{ t('agents.cfgModelSub') }}</span></div>
            <div class="frow">
              <div class="f-lab">{{ t('agents.cfgModel') }}</div>
              <div class="f-val">
                <span class="code">{{ cur.engine_model || '—' }}</span>
                <span class="f-lock"><Icon name="lock" :size="12" />{{ t('agents.cfgReadonly') }}</span>
              </div>
            </div>
            <div class="f-hint">
              {{ t('agents.cfgModelHint') }}
              <a :href="qwenpawUrl" target="_blank" rel="noopener">{{ t('agents.cfgOpenQwenpaw') }} ↗</a>
            </div>
            <div class="card-h" style="margin-top:var(--gap)">
              <b>{{ t('agents.cfgParams') }}</b>
            </div>
            <div class="frow">
              <div class="f-lab">{{ t('agents.cfgTemp') }}</div>
              <el-input v-model="form.params.temperature" size="small" placeholder="0.7" />
            </div>
            <div class="frow">
              <div class="f-lab">{{ t('agents.cfgMaxTokens') }}</div>
              <el-input v-model="form.params.max_tokens" size="small" placeholder="—" />
            </div>
            <div class="frow">
              <div class="f-lab">{{ t('agents.cfgTopP') }}</div>
              <el-input v-model="form.params.top_p" size="small" placeholder="—" />
            </div>
            <div class="frow" style="align-items:flex-start">
              <div class="f-lab">{{ t('agents.cfgSuffix') }}</div>
              <el-input v-model="form.params.system_suffix" type="textarea" :rows="3" resize="none" />
            </div>
            <div class="f-hint">{{ t('agents.cfgSuffixHint') }}</div>
          </div>

          <div class="card" style="padding:14px">
            <div class="card-h">
              <b>{{ t('agents.cfgExec') }}</b>
              <span class="sub"><Icon name="lock" :size="12" />{{ t('agents.cfgExecSub') }}</span>
            </div>
            <div class="frow"><div class="f-lab">{{ t('agents.cfgEngine') }}</div>
              <div class="f-val">{{ cur.engine?.type }} · {{ cur.engine?.base_url }}</div></div>
            <div class="frow"><div class="f-lab">{{ t('agents.cfgWorkspace') }}</div>
              <div class="f-val">{{ cur.engine?.agent_id }}</div></div>
            <div class="f-row-note">{{ t('agents.cfgCapabilityHint') }}</div>
            <div class="f-hint">
              {{ t('agents.cfgPromptTitle') }}：{{ t('agents.cfgPromptHint') }}
              <a :href="qwenpawUrl" target="_blank" rel="noopener">{{ t('agents.cfgOpenQwenpaw') }} ↗</a>
            </div>
          </div>

          <div class="card" style="padding:14px">
            <div class="card-h"><b>{{ t('agents.cfgAccess') }}</b><span class="sub">{{ t('agents.cfgAccessSub') }}</span></div>
            <div class="frow">
              <div class="f-lab">{{ t('agents.cfgAccess') }}</div>
              <div class="f-val">
                {{ t('agents.cfgAccessAdmin') }}
                <span class="f-lock"><Icon name="lock" :size="12" />{{ t('agents.cfgReadonly') }}</span>
              </div>
            </div>
            <div class="f-hint">{{ t('agents.cfgAccessHint') }}</div>
          </div>
        </div>

        <!-- ③ 运行记录 -->
        <div v-if="tab === 'runs'">
          <div class="card" style="padding:14px">
            <div class="card-h"><b>{{ t('agents.runsDetail') }}</b><span class="sub">{{ t('agents.runsDetailSub') }}</span></div>
            <el-table v-if="runList.length" :data="runList" size="small" style="margin-top:var(--gap-s)">
              <el-table-column prop="at" :label="t('agents.colTime')" width="160" />
              <el-table-column prop="account" :label="t('agents.colAccount')" width="110" />
              <el-table-column prop="rounds" :label="t('agents.colRounds')" width="80" />
              <el-table-column prop="tools" :label="t('agents.colTools')" width="90" />
              <el-table-column prop="tokens" :label="t('agents.colTokens')" width="100" />
              <el-table-column :label="t('agents.colMs')" width="90">
                <template #default="{ row }">{{ (row.ms / 1000).toFixed(1) }}s</template>
              </el-table-column>
              <el-table-column :label="t('agents.colStatus')">
                <template #default="{ row }">
                  <span class="pill" :class="runStatusCls(row.status)">{{ runStatusText(row.status) }}</span>
                </template>
              </el-table-column>
            </el-table>
            <div v-else class="hint">{{ t('agents.runsEmpty') }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 新建弹窗 -->
    <el-dialog v-model="createOpen" :title="t('agents.newAgent')" width="520px">
      <div class="shared-note">{{ t('agents.sharedNote') }}</div>
      <div class="frow">
        <div class="f-lab">{{ t('agents.formName') }}</div>
        <el-input v-model="create.display_name" size="small" />
      </div>
      <div class="frow">
        <div class="f-lab">{{ t('agents.formSlug') }}</div>
        <el-input v-model="create.slug" size="small" placeholder="code-review" />
      </div>
      <div class="frow">
        <div class="f-lab">{{ t('agents.formDesc') }}</div>
        <el-input v-model="create.description" size="small" />
      </div>
      <div class="frow" style="align-items:flex-start">
        <div class="f-lab">{{ t('agents.formSuffix') }}</div>
        <el-input v-model="create.system_suffix" type="textarea" :rows="2" resize="none" />
      </div>
      <template #footer>
        <el-button size="small" @click="createOpen = false">{{ t('agents.cancel') }}</el-button>
        <el-button size="small" type="primary" @click="doCreate">{{ t('agents.create') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import Icon from '../components/Icon.vue'
import {
  type AgentEvent, type AgentItem, type AgentRun,
  agentRuns, agentSessions, createAgent, listAgents,
  restoreAgent, retireAgent, streamAgentChat, testAgent, updateAgent,
} from '../lib/agents'
import { renderMarkdown } from '../lib/markdown'
import { PRESETS, pickImage } from '../lib/upload'

const { t } = useI18n()

interface Msg { id: number; role: 'user' | 'assistant'; text: string; steps: Step[]; open?: boolean; meta?: string; images?: string[] }
interface Step { id: string; name?: string; args?: any; status: 'running' | 'done'; ms?: number; output?: any }

const err = ref('')
const data = reactive<{ agents: AgentItem[]; max_agents: number; active: number; engine: any }>({
  agents: [], max_agents: 5, active: 0,
  engine: { model: '', ready: false, base_url: '', agent_id: '' },
})
const showRetired = ref(false)
const current = ref('')
const tab = ref<'chat' | 'config' | 'runs'>('chat')
const runList = ref<AgentRun[]>([])

const messages = ref<Msg[]>([])
const sessions = ref<any[]>([])
const sessionId = ref('')
const input = ref('')
const pendingImgs = ref<{ uri: string; name: string; thumb: string }[]>([])
const MAX_IMGS = 4
const fileInput = ref<HTMLInputElement | null>(null)
const streaming = ref(false)
const stepsDone = ref(0)
const lastUsage = ref<any>(null)
const startedAt = ref(0)
const elapsedText = ref('—')
const scrollEl = ref<HTMLElement | null>(null)
let abort: (() => void) | null = null

const form = reactive({ display_name: '', description: '', params: { temperature: '', max_tokens: '', top_p: '', system_suffix: '' } as any })
const createOpen = ref(false)
const create = reactive({ display_name: '', slug: '', description: '', system_suffix: '' })
const testing = ref(false)
const testResult = ref<{ ok: boolean; ms: number; detail: string } | null>(null)

const cur = computed(() => data.agents.find((a) => a.slug === current.value) || null)
const hasRetired = computed(() => data.agents.some((a) => a.status === 'retired'))
const activeCount = computed(() => data.agents.filter((a) => a.status !== 'retired').length)
const atLimit = computed(() => activeCount.value >= data.max_agents)
const totalRuns = computed(() => data.agents.reduce((s, a) => s + (a.run_total || 0), 0))
const totalTokensText = computed(() => `${(totalRuns.value * 12).toFixed(0)}k`)
const qwenpawUrl = computed(() => (data.engine.base_url || 'http://127.0.0.1:8088').replace(/\/api$/, ''))
const curSessionTitle = computed(() => sessions.value.find((s) => s.id === sessionId.value)?.title || t('agents.newChat'))
const dirty = computed(() => {
  const c = cur.value; if (!c) return false
  const p = c.params || {}
  return form.display_name !== c.display_name || form.description !== c.description
    || String(form.params.temperature ?? '') !== String(p.temperature ?? '')
    || String(form.params.max_tokens ?? '') !== String(p.max_tokens ?? '')
    || String(form.params.top_p ?? '') !== String(p.top_p ?? '')
    || String(form.params.system_suffix ?? '') !== String(p.system_suffix ?? '')
})

function statusText(s: string) {
  return s === 'retired' ? t('agents.statusRetired') : s === 'draft' ? t('agents.statusDraft') : t('agents.statusPublished')
}
function runStatusText(s: string) {
  return s === 'ok' ? t('agents.runOk') : s === 'stopped' ? t('agents.runStopped') : s === 'error' ? t('agents.runTimeout') : s
}
function runStatusCls(s: string) { return s === 'ok' ? 'ok' : s === 'error' ? 'err' : 'mute' }
function render(md: string) { return renderMarkdown(md || '') }
function stepDetail(st: Step) {
  if (st.status === 'done' && st.output != null) return String(st.output).slice(0, 120)
  if (st.args) { try { return JSON.stringify(st.args).slice(0, 120) } catch { return '' } }
  return ''
}

async function load() {
  err.value = ''
  try {
    const j = await listAgents(showRetired.value)
    data.agents = j.agents; data.max_agents = j.max_agents; data.active = j.active; data.engine = j.engine
    if (!cur.value) {
      const first = data.agents.find((a) => a.status !== 'retired')
      if (first) pick(first.slug)
    }
  } catch (e: any) { err.value = String(e?.message || e) }
}

function syncForm(a: AgentItem) {
  form.display_name = a.display_name
  form.description = a.description || ''
  const p = a.params || {}
  form.params.temperature = p.temperature ?? ''
  form.params.max_tokens = p.max_tokens ?? ''
  form.params.top_p = p.top_p ?? ''
  form.params.system_suffix = p.system_suffix ?? ''
}

async function pick(slug: string) {
  current.value = slug
  tab.value = 'chat'
  const a = cur.value
  if (a) syncForm(a)
  sessionId.value = ''
  messages.value = []
  await loadSessions()
}

async function loadSessions() {
  if (!current.value) return
  try {
    const j = await agentSessions(current.value)
    sessions.value = (j.items || []).map((it: any) => ({
      id: it.id, title: it.title || t('agents.runDefaultTitle'), time: it.time || '', n: (it.messages || []).length, raw: it,
    }))
  } catch { sessions.value = [] }
}

function newChat() {
  sessionId.value = ''
  messages.value = []
  stepsDone.value = 0
  lastUsage.value = null
  elapsedText.value = '—'
}

function switchSession(id: string) {
  const s = sessions.value.find((x) => x.id === id)
  sessionId.value = id
  messages.value = (s?.raw?.messages || []).map((m: any, i: number) => ({
    id: i, role: m.role === 'user' ? 'user' : 'assistant', text: m.content || '', steps: [], meta: '',
  }))
}

async function persistSession() {
  if (!current.value || !messages.value.length) return
  const body = {
    id: sessionId.value || `s-${Date.now()}`,
    title: (messages.value.find((m) => m.role === 'user')?.text || t('agents.runDefaultTitle')).slice(0, 24),
    time: new Date().toLocaleString('zh-CN', { hour12: false }).slice(5, 16),
    messages: messages.value.map((m) => ({ role: m.role, content: m.text })),
  }
  sessionId.value = body.id
  try {
    await fetch(`/history/agent_${current.value}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${localStorage.getItem('gw_token') || ''}` },
      body: JSON.stringify({ items: [body] }),
    })
    await loadSessions()
  } catch { /* 持久化失败不阻塞对话 */ }
}

function send() {
  const text = input.value.trim()
  // 允许"只发图不带字"
  if ((!text && !pendingImgs.value.length) || streaming.value || !current.value) return
  err.value = ''
  const imgs = pendingImgs.value.map((x) => x.uri)
  pendingImgs.value = []
  messages.value.push({ id: Date.now(), role: 'user', text, steps: [], images: imgs })
  const reply: Msg = { id: Date.now() + 1, role: 'assistant', text: '', steps: [], open: true, meta: '' }
  messages.value.push(reply)
  input.value = ''
  streaming.value = true
  stepsDone.value = 0
  lastUsage.value = null
  startedAt.value = Date.now()
  elapsedText.value = '—'
  scroll()

  const history = messages.value
    .filter((m) => m !== reply)
    .map((m) => {
      const im = (m.images || []).filter(Boolean)
      if (!im.length) return { role: m.role, content: m.text }
      const parts: any[] = []
      if (m.text) parts.push({ type: 'text', text: m.text })
      for (const url of im) parts.push({ type: 'image', image_url: url })
      return { role: m.role, content: parts }
    })

  abort = streamAgentChat(current.value, history, sessionId.value || `agent-${current.value}-${Date.now()}`, (ev: AgentEvent) => {
    if (ev.type === 'start') { sessionId.value = ev.session_id }
    else if (ev.type === 'text') { reply.text += ev.text; scroll() }
    else if (ev.type === 'step') {
      if (ev.status === 'running') reply.steps.push({ id: ev.id, name: ev.name, args: ev.args, status: 'running' })
      else {
        const st = reply.steps.find((x) => x.id === ev.id)
        if (st) { st.status = 'done'; st.ms = ev.ms; st.output = ev.output }
        stepsDone.value++
      }
      scroll()
    } else if (ev.type === 'usage') {
      lastUsage.value = ev
      reply.meta = `${ev.model || ''} · ${ev.prompt + ev.completion} tokens`
        + (ev.cached ? ` · ${t('agents.cacheHit', { n: ev.cached })}` : '')
    } else if (ev.type === 'done') {
      reply.open = false
      finish()
    } else if (ev.type === 'error') {
      err.value = ev.message
      reply.text += `\n\n> ⚠️ ${ev.message}`
      finish()
    }
  })
}

async function onFiles(ev: Event) {
  const el = ev.target as HTMLInputElement
  for (const f of Array.from(el.files || [])) {
    if (pendingImgs.value.length >= MAX_IMGS) {
      ElMessage.warning(t('agents.imgMax', { n: MAX_IMGS }))
      break
    }
    try {
      const m = await pickImage(f, PRESETS.chat)
      pendingImgs.value.push({ uri: m.uri, name: m.name, thumb: m.thumb })
    } catch (e: any) {
      ElMessage.error(t('agents.imgErr', { msg: (e && e.message) || e }))
    }
  }
  if (fileInput.value) fileInput.value.value = ''
}
function removeImg(i: number) { pendingImgs.value.splice(i, 1) }

function finish() {
  streaming.value = false
  elapsedText.value = `${((Date.now() - startedAt.value) / 1000).toFixed(1)}s`
  abort = null
  persistSession()
  scroll()
}

function stopLocal() {
  if (abort) abort()
  streaming.value = false
  elapsedText.value = `${((Date.now() - startedAt.value) / 1000).toFixed(1)}s`
}

function scroll() {
  nextTick(() => { if (scrollEl.value) scrollEl.value.scrollTop = scrollEl.value.scrollHeight })
}

function copyAll() {
  const text = messages.value.map((m) => `${m.role === 'user' ? t('agents.meLabel') : t('agents.aiLabel')}：${m.text}`).join('\n\n')
  navigator.clipboard?.writeText(text)
}

async function saveCfg() {
  if (!cur.value) return
  err.value = ''
  const patch: any = { display_name: form.display_name, description: form.description, params: {} }
  for (const k of ['temperature', 'max_tokens', 'top_p'] as const) {
    const v = form.params[k]
    if (v !== '' && v != null) patch.params[k] = Number(v)
  }
  if (form.params.system_suffix) patch.params.system_suffix = form.params.system_suffix
  try {
    await updateAgent(cur.value.slug, patch)
    await load()
    await pick(cur.value.slug)
  } catch (e: any) { err.value = String(e?.message || e) }
}

async function doTest() {
  if (!cur.value) return
  testing.value = true; testResult.value = null
  try { testResult.value = await testAgent(cur.value.slug) }
  catch (e: any) { testResult.value = { ok: false, ms: 0, detail: String(e?.message || e) } }
  finally { testing.value = false }
}

async function doRetire() {
  if (!cur.value || !confirm(t('agents.confirmDelete'))) return
  try { await retireAgent(cur.value.slug); await load(); pick(data.agents[0]?.slug || '') }
  catch (e: any) { err.value = String(e?.message || e) }
}

async function doRestore() {
  if (!cur.value) return
  try { await restoreAgent(cur.value.slug); await load(); pick(cur.value.slug) }
  catch (e: any) { err.value = String(e?.message || e) }
}

function openCreate() {
  create.display_name = ''; create.slug = ''; create.description = ''; create.system_suffix = ''
  createOpen.value = true
}

async function doCreate() {
  err.value = ''
  const body: any = {
    display_name: create.display_name, slug: create.slug.trim(),
    description: create.description,
  }
  if (create.system_suffix) body.params = { system_suffix: create.system_suffix }
  try {
    const j = await createAgent(body)
    createOpen.value = false
    await load()
    await pick(j.agent.slug)
  } catch (e: any) { err.value = String(e?.message || e) }
}

async function loadRuns() {
  if (!current.value) return
  try { runList.value = (await agentRuns(current.value)).items || [] }
  catch { runList.value = [] }
}

onMounted(load)
</script>

<style scoped>
.sub-hint { margin-left: 8px; color: var(--muted); font-size: 12.5px; }
.row-h { display: flex; align-items: center; gap: var(--gap-sm); margin-bottom: var(--gap-lg); }
.ag-layout {
  display: grid; grid-template-columns: 320px minmax(0, 1fr);
  gap: var(--gap-lg); align-items: start;
}
/* 左列表卡：与右侧详情同高（同排对齐），内部列表区滚动 */
.ag-layout > .card:first-child {
  align-self: stretch; display: flex; flex-direction: column;
  min-height: 320px;
}
.ag-layout > .card:first-child .ag-list { flex: 1; min-height: 0; overflow: auto; }
/* KPI 卡等高（第三十八轮：之前几张高度不一，因为内容行数不同） */
.ag-kpis { align-items: stretch; margin-bottom: var(--gap-lg); }
.ag-kpis .kpi .v { line-height: 1.15; }
/* 注脚统一贴底、单行省略 —— 否则第一张卡注脚两行会把四张卡的文字基线拉歪 */
.ag-kpis .kpi .f {
  margin-top: auto; padding-top: 8px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ag-kpis .kpi { display: flex; flex-direction: column; }
.ag-layout > * { min-height: 0; }
.ag-list-h { flex-wrap: wrap; margin-bottom: var(--gap-s); }
.ag-list-h b { flex: none; }
.ag-list-h .sub { flex: 1 1 100%; font-size: 11px; line-height: 1.5; }
.ag-list { display: flex; flex-direction: column; gap: 2px; }
.ag-list-foot { margin-top: auto; padding-top: var(--gap); font-size: 11px; color: var(--muted-2); line-height: 1.6; }
.ag { display: flex; gap: 10px; align-items: flex-start; padding: 10px; border-radius: var(--radius); border: 1px solid transparent; cursor: pointer; }
.ag:hover { background: var(--surface-2); }
.ag.on { background: var(--primary-soft); border-color: color-mix(in srgb, var(--primary) 32%, transparent); }
.ag-ico { width: 34px; height: 34px; flex: none; border-radius: 10px; display: grid; place-items: center; background: var(--chip); color: var(--primary); border: 1px solid var(--line); }
.ag-main { min-width: 0; flex: 1; }
.ag-nm { font-weight: 650; font-size: 13.5px; }
.ag-ds { font-size: 12px; color: var(--muted); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ag-tags { display: flex; align-items: center; gap: 6px; margin-top: 7px; }
.ag-md { font-family: ui-monospace, Menlo, monospace; font-size: 11px; color: var(--muted-2); }
.ag-right { flex: none; }
.ag-empty { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; text-align: center; padding: var(--pad-page); min-height: 340px; }
.ae-ico { width: 64px; height: 64px; border-radius: 18px; display: grid; place-items: center; background: var(--primary-soft); color: var(--primary); }
.ae-t { font-weight: 650; font-size: 14.5px; }
.ae-s { font-size: 12.5px; color: var(--muted); line-height: 1.7; }
.ae-btn { margin-top: 8px; display: inline-flex; align-items: center; gap: 6px; padding: 7px 14px; border-radius: 10px; font-size: 12.5px; font-weight: 600; background: var(--primary); color: var(--primary-ink); cursor: pointer; }
.ag-detail { min-width: 0; display: flex; flex-direction: column; gap: var(--gap-lg); }
.ag-head { padding: 12px 14px; }
.ag-head .row-h { margin-bottom: 0; }
.ag-head .ag-tabs { margin-top: var(--gap-s); }
.ag-top { display: flex; align-items: center; gap: 9px; }
.ag-face { width: 28px; height: 28px; border-radius: 9px; display: grid; place-items: center; background: var(--primary-soft); color: var(--primary); flex: none; }
.ag-tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--line); }
.ag-tabs span { padding: 7px 14px; font-size: 12.5px; font-weight: 600; color: var(--muted); border-bottom: 2px solid transparent; margin-bottom: -1px; cursor: pointer; }
.ag-tabs span.on { color: var(--primary); border-bottom-color: var(--primary); }
.risk-bar { display: flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--warn); background: var(--warn-soft); padding: 6px 10px; border-radius: var(--radius-s); margin-bottom: var(--gap-sm); }
.side-in { padding: 12px; display: flex; flex-direction: column; overflow: hidden; height: 100%; border-right: 1px solid var(--line-2); }
/* 对话区整体一张卡：左栏用分隔线区分，不再各自成块 → 不会有高度/圆角错位 */
.chat-layout {
  display: grid; grid-template-columns: 224px minmax(0, 1fr);
  gap: 0; align-items: stretch;
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); overflow: hidden;
  height: calc(100vh - 268px); min-height: 420px;
}
.new-conv { width: 100%; margin-bottom: 10px; }
.conv-list { flex: 0 1 auto; overflow: auto; min-height: 0; max-height: 42%; }
.conv { padding: 7px 9px; border-radius: var(--radius-s); font-size: 12.5px; cursor: pointer; }
.conv small { display: block; color: var(--muted-2); font-size: 11px; margin-top: 2px; }
.conv.on { background: var(--primary-soft); }
.chat-main { display: flex; flex-direction: column; min-width: 0; height: calc(100vh - 268px); min-height: 420px; }
.chat-layout > * { min-height: 0; }
.chat-card { flex: 1; display: flex; flex-direction: column; overflow: hidden; padding: 12px 14px; }
.msgs { flex: 1; overflow: auto; margin-top: var(--gap); min-height: 0; }
.trace { border: 1px solid var(--line); border-left: 2px solid var(--primary); border-radius: 10px; padding: 8px 10px; margin-bottom: 9px; background: var(--surface-2); }
.trace-h { display: flex; align-items: center; gap: 6px; font-size: 11.5px; font-weight: 600; color: var(--primary); }
.trace-caret { margin-left: auto; color: var(--muted-2); cursor: pointer; }
.step { display: flex; align-items: center; gap: 7px; font-size: 11.5px; padding: 4px 0 0; color: var(--muted); }
.step.ok svg { color: var(--ok); }
.step.running svg { color: var(--warn); }
.s-nm { font-family: ui-monospace, Menlo, monospace; font-size: 11px; color: var(--text); }
.s-dt { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.s-ms { margin-left: auto; color: var(--muted-2); }
.composer { border-top: 1px solid var(--line-2); padding-top: var(--gap-sm); }
.bar { display: flex; align-items: center; gap: var(--gap-sm); margin-top: 8px; }
.tok { font-size: 11.5px; color: var(--muted-2); font-family: ui-monospace, Menlo, monospace; }
.stop-btn { background: var(--err-soft); color: var(--err); border-color: transparent; }
.typing { color: var(--muted-2); }
.msg-imgs { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.msg-imgs img { max-width: 132px; max-height: 132px; border-radius: var(--radius-s); border: 1px solid var(--line); display: block; }
.pending { display: flex; align-items: center; gap: 8px; margin-top: var(--gap-sm); }
.pend-i { position: relative; }
.pend-i img { width: 46px; height: 46px; object-fit: cover; border-radius: var(--radius-sm, 8px); border: 1px solid var(--line); display: block; }
.pend-x { position: absolute; top: -6px; right: -6px; width: 17px; height: 17px; border-radius: 50%; background: var(--err); color: #fff; font-size: 12px; line-height: 17px; text-align: center; cursor: pointer; }
.pend-hint { font-size: 11px; color: var(--muted-2); }
.clip-btn { background: transparent; border: 0; color: var(--muted-2); cursor: pointer; padding: 4px; display: grid; place-items: center; }
.clip-btn:disabled { opacity: .4; cursor: not-allowed; }
.clip-btn:hover:not(:disabled) { color: var(--primary); }
.cfg-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--gap-lg); align-items: start; }
.cfg-grid > .card:first-child { grid-column: 1 / -1; }
.frow { display: flex; align-items: center; gap: 10px; margin-top: var(--gap-s); }
.f-lab { width: 78px; flex: none; font-size: 12px; color: var(--muted); }
.f-val { flex: 1; min-width: 0; display: flex; align-items: center; gap: 7px; font-size: 12.5px; padding: 7px 10px; border-radius: 9px; background: var(--chip); }
.f-val.code { font-family: ui-monospace, Menlo, monospace; font-size: 11.5px; }
.f-lock { margin-left: auto; display: inline-flex; align-items: center; gap: 4px; font-size: 10.5px; color: var(--muted-2); }
.f-hint { font-size: 11px; color: var(--muted-2); line-height: 1.6; margin: var(--gap-xs) 0 0 88px; }
.f-row-note { font-size: 11.5px; color: var(--muted); line-height: 1.6; margin-top: var(--gap-s); }
.row-actions { display: flex; align-items: center; gap: var(--gap-sm); margin-top: var(--gap); }
.test-res { font-size: 11.5px; color: var(--ok); }
.test-res.bad { color: var(--err); }
.shared-note { font-size: 12px; color: var(--muted); background: var(--chip); padding: 8px 10px; border-radius: var(--radius-s); margin-bottom: var(--gap-s); }
.ops-row { display: flex; gap: 7px; margin-top: 12px; }
@media (max-width: 768px) {
  .ag-layout { grid-template-columns: 1fr; }
  .chat-layout { grid-template-columns: 1fr; height: auto; }
  .chat-side { display: none; }
  .cfg-grid { grid-template-columns: 1fr; }
  .cfg-grid > .card:first-child { grid-column: auto; }
}
</style>
