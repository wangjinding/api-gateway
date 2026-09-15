<template>
  <div class="page-fill">
    <div class="chat-layout">
      <!-- 左栏：会话。窄屏由 style.css 隐藏（.chat-side），改用右栏顶部的会话下拉 -->
      <aside class="chat-side">
        <div class="card side-in">
          <el-button type="primary" class="new-conv" :disabled="streaming" @click="clearConversation">
            <Icon name="plus" :size="14" />
            <span>{{ t('chat.newChat') }}</span>
          </el-button>

          <!-- 会话列表：当前这条（服务端 history/chat 的 current 条目）加 .on -->
          <div class="conv-list">
            <div v-for="c in conversations" :key="c.id" class="conv" :class="{ on: c.id === activeConvId }">
              {{ c.title }}
              <small>{{ c.time }} · {{ t('chat.msgsN', { n: c.n }) }}</small>
            </div>
          </div>

          <div class="sep" />

          <div class="card-h">
            <b>{{ t('chat.thisConv') }}</b>
          </div>
          <div class="pv conv-info">
            <div class="pv-i">
              <span class="nm">{{ t('chat.model') }}</span>
              <span class="st code">{{ params.model }}</span>
            </div>
            <div class="pv-i">
              <span class="nm">Tokens</span>
              <span class="st">{{ totalTokens }}</span>
            </div>
            <div class="pv-i">
              <span class="nm">{{ t('chat.elapsed') }}</span>
              <span class="st">{{ elapsedText }}</span>
            </div>
          </div>

          <div class="side-ops">
            <el-button class="op" size="small" :disabled="!messages.length" @click="exportConv">
              <Icon name="dl" :size="13" />
              <span>{{ t('common.export') }}</span>
            </el-button>
            <el-button class="op" size="small" :disabled="streaming || !messages.length" @click="clearConversation">
              <Icon name="trash" :size="13" />
              <span>{{ t('chat.clear') }}</span>
            </el-button>
          </div>
        </div>
      </aside>

      <!-- 右栏：参数胶囊 + 消息 + 输入区 -->
      <section class="chat-main">
        <div class="chat-top">
          <!-- 窄屏没有左栏，会话切换收在这里（只有一条会话时不显示） -->
          <el-select
            v-if="isMobile && conversations.length > 1"
            v-model="activeConvId"
            size="small"
            class="conv-sel"
            :placeholder="t('chat.session')"
          >
            <el-option v-for="c in conversations" :key="c.id" :value="c.id" :label="c.title" />
          </el-select>

          <span class="chip-sel" :class="{ on: params.stream }" @click="paramsOpen = true">
            <Icon name="bolt" :size="13" />{{ t('chat.streaming') }}
          </span>
          <span class="chip-sel" @click="paramsOpen = true">{{ t('chat.tempShort') }} {{ params.temperature }}</span>
          <span class="chip-sel" @click="paramsOpen = true">
            {{ t('chat.maxShort') }} {{ params.max_tokens ?? t('common.none') }}
          </span>
          <span class="chip-sel" :class="{ on: !!params.system.trim() }" @click="paramsOpen = true">
            {{ t('chat.systemShort') }}
          </span>

          <span v-if="streaming" class="stream-badge">{{ t('chat.generating') }}</span>
          <span class="vision-hint" :class="{ off: !visionOk }">
            {{ visionOk ? t('chat.visionReady') : t('chat.visionOff') }}
          </span>
        </div>

        <div class="card chat-card">
          <div class="card-h">
            <b>{{ convTitle }}</b>
            <span class="sub">{{ t('chat.msgsN', { n: messages.length }) }}<template v-if="lastAt"> · {{ fmtClock(lastAt) }}</template></span>
            <div class="pager">
              <el-button size="small" :disabled="streaming || !lastUser" @click="regenerate">
                <Icon name="refresh" :size="13" />
                <span>{{ t('chat.regenerate') }}</span>
              </el-button>
              <el-button size="small" :disabled="!messages.length" @click="copyAll">
                <Icon name="copy" :size="13" />
                <span>{{ t('chat.copyAll') }}</span>
              </el-button>
            </div>
          </div>

          <div ref="scrollEl" class="msgs" @scroll="onScroll">
            <div v-if="messages.length === 0" class="empty">
              <Icon name="chat" :size="34" />
              <div class="empty-t">{{ t('chat.empty') }}</div>
            </div>

            <div v-for="msg in messages" :key="msg.id" class="msg" :class="msg.role === 'user' ? 'me' : 'ai'">
              <!-- 助手：降级/服务信息 -->
              <div v-if="msg.role === 'assistant' && msg.meta" class="resp-meta">
                <el-tag size="small" :type="msg.meta.fallback ? 'warning' : 'success'" effect="light">
                  <template v-if="msg.meta.fallback">{{ t('chat.fallback', { provider: msg.meta.provider }) }}</template><template v-else>{{ t('chat.servedBy', { provider: msg.meta.provider }) }}</template>
                </el-tag>
                <span class="meta-txt">
                  <template v-if="msg.meta.fallback">{{ t('chat.requestedTo', { req: msg.meta.requested, served: msg.meta.served }) }}</template>
                  <template v-else>{{ msg.meta.served }}</template>
                </span>
                <span v-if="msg.usage || msg.cost != null" class="meta-txt">
                  · {{ msg.cost != null ? `¥${msg.cost}` : '' }}
                  <template v-if="msg.usage">{{ tokensText(msg.usage) }}</template>
                </span>
              </div>

              <!-- 助手：推理折叠 -->
              <el-collapse v-if="msg.role === 'assistant' && msg.reasoning" class="reasoning">
                <el-collapse-item :title="t('chat.reasoning')" name="r">
                  <div class="reasoning-md">{{ msg.reasoning }}</div>
                </el-collapse-item>
              </el-collapse>

              <!-- 附图缩略（用户消息） -->
              <div v-if="msg.images && msg.images.length" class="attach">
                <el-image v-for="(u, k) in msg.images" :key="k" :src="u" fit="cover"
                  :title="msg.imgNames && msg.imgNames[k]"
                  :preview-src-list="msg.images" :initial-index="k" preview-teleported />
              </div>

              <template v-if="msg.role === 'user'">
                <div v-if="msg.content" class="plain">{{ msg.content }}</div>
              </template>
              <template v-else>
                <div v-if="msg.content" class="md" v-html="renderMarkdown(msg.content)"></div>
                <div v-else-if="!msg.error && !msg.reasoning" class="placeholder">{{ msg.streaming ? t('chat.thinking') : t('chat.emptyResp') }}</div>
              </template>
              <span v-if="msg.streaming && !msg.error" class="cursor">▍</span>

              <div v-if="metaText(msg)" class="meta">{{ metaText(msg) }}</div>

              <div v-if="msg.error" class="err">
                <el-alert :title="t('chat.youError', { msg: msg.error })" type="error" :closable="false" show-icon />
              </div>
            </div>
          </div>
        </div>

        <!-- 输入区 -->
        <div class="composer">
          <div class="in">
            <!-- 第十三轮: 视觉对话 —— 附图（上传的图片在浏览器内压缩成 data URI；也支持粘 URL） -->
            <div v-if="pending.length" class="img-bar">
              <el-tag v-for="(im, i) in pending" :key="i" closable type="info" size="small" class="img-chip"
                :title="im.uri" @close="pending.splice(i, 1)">{{ im.name }}</el-tag>
            </div>
            <div v-if="showImg" class="img-row">
              <el-input v-model="imgUrl" size="small" :placeholder="t('chat.imgPh')" @keyup.enter="addImg">
                <template #append><el-button @click="addImg">{{ t('chat.imgAdd') }}</el-button></template>
              </el-input>
            </div>
            <!-- 带图但所选模型没有视觉能力时提前说清楚（否则上游要么忽略图片、要么报参数错误） -->
            <el-alert v-if="pending.length && !visionOk" type="warning" show-icon :closable="false" class="vision-warn"
              :title="t('chat.visionWarn', { model: params.model })">
              <template #default>
                <el-button v-if="firstVisionModel" size="small" text type="primary" @click="useVisionModel">
                  {{ t('chat.useVision', { model: firstVisionModel }) }}
                </el-button>
              </template>
            </el-alert>

            <el-input
              v-model="text"
              type="textarea"
              :rows="2"
              resize="none"
              :placeholder="t('chat.placeholder')"
              :disabled="streaming"
              @keydown="onKeydown"
              @focus="onInputFocus"
            />
          </div>

          <div class="bar">
            <el-tooltip :content="t('chat.imgUpload')" placement="top">
              <el-button text size="small" :disabled="streaming" @click="pickImages">
                <Icon name="clip" :size="16" />
              </el-button>
            </el-tooltip>
            <el-tooltip :content="t('chat.imgUrlBtn')" placement="top">
              <el-button text size="small" @click="showImg = !showImg">
                <Icon name="link" :size="16" />
              </el-button>
            </el-tooltip>
            <input ref="fileEl" type="file" accept="image/*" multiple class="hide-file" @change="onFiles" />

            <el-select v-model="params.model" class="model-sel" size="small" :disabled="streaming" filterable :placeholder="t('chat.routeGroup')">
              <el-option-group :label="t('chat.routeGroup')">
                <el-option v-for="r in routes" :key="r.id" :value="r.id" :label="chainLabel(r)">
                  <span class="opt">
                    <span>{{ chainLabel(r) }}</span>
                    <Icon v-if="r.vision" name="eye" :size="13" />
                  </span>
                </el-option>
              </el-option-group>
              <el-option-group :label="t('chat.textModels')">
                <el-option v-for="m in textModels" :key="m.id" :value="m.id" :label="modelLabel(m)">
                  <span class="opt">
                    <span>{{ modelLabel(m) }}</span>
                    <Icon v-if="m.vision" name="eye" :size="13" />
                  </span>
                </el-option>
              </el-option-group>
            </el-select>

            <span v-if="!streaming && !text.trim()" class="hint">{{ t('chat.waiting') }}</span>
            <span class="tok">{{ totalTokens }} Tokens</span>

            <el-button v-if="!streaming" type="primary" size="small" :disabled="!text.trim() && !pending.length" @click="send">
              <Icon name="send" :size="14" />
              <span>{{ t('chat.send') }}</span>
            </el-button>
            <el-button v-else type="danger" plain size="small" @click="stop">
              <Icon name="stop" :size="14" />
              <span>{{ t('chat.stop') }}</span>
            </el-button>
          </div>
        </div>
      </section>
    </div>

    <!-- 参数抽屉：窄屏按屏宽百分比，避免 330px 在小屏上顶到边 -->
    <el-drawer v-model="paramsOpen" :title="t('chat.params')" :size="isMobile ? '86%' : '330px'" :close-on-click-modal="true">
      <div class="param">
        <div class="plabel">{{ t('chat.temp') }}</div>
        <el-slider v-model="params.temperature" :min="0" :max="2" :step="0.05" show-input />
      </div>
      <div class="param">
        <div class="plabel">{{ t('chat.topP') }}</div>
        <el-slider v-model="params.top_p" :min="0" :max="1" :step="0.05" show-input />
      </div>
      <div class="param">
        <div class="plabel">{{ t('chat.maxTokens') }}</div>
        <el-input-number v-model="params.max_tokens" :min="1" :max="200000" :placeholder="t('chat.phDefault')" clearable style="width: 100%" />
      </div>
      <div class="param">
        <div class="plabel">{{ t('chat.streaming') }}</div>
        <el-switch v-model="params.stream" :active-text="t('common.yes')" :inactive-text="t('common.no')" />
      </div>
      <div class="param">
        <div class="plabel">{{ t('chat.system') }}</div>
        <el-input v-model="params.system" type="textarea" :rows="4" resize="none" :placeholder="t('chat.phOptional')" />
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { api } from '../api'
import Icon from '../components/Icon.vue'
import { renderMarkdown } from '../lib/markdown'
import { streamChat, chatOnce, buildPayloadMessages, nextId, seekId, type ChatMessage } from '../lib/chat'
import { getChatDefaults } from '../lib/settings'
import { pullHistory, pushHistory } from '../lib/history'
import { stamp } from '../lib/download'
import { useIsMobile } from '../lib/ui'
import { pickImage, PRESETS, assertBodyBudget } from '../lib/upload'

const { isMobile } = useIsMobile()

const router = useRouter()
const { t } = useI18n()

// ── 模型选项 ──
const routes = ref<any[]>([])
const textModels = ref<any[]>([])
async function loadModels() {
  try {
    const j = await api('/v1/models')
    const data: any[] = j.data || []
    routes.value = data.filter((x) => x.type === 'route')
    textModels.value = data.filter((x) => x.type !== 'route')
  } catch (e: any) {
    if (e && e.status === 401) { router.push('/login'); return }
    ElMessage.error(t('chat.loadModelsErr', { msg: (e && e.message) || e }))
  }
}
function chainLabel(r: any) {
  const chain = (r.models || []).join(' → ')
  // 括号样式（全角/半角）也交给 i18n：英文里用半角更自然
  return t('chat.chainLabel', { id: r.id, chain: chain || t('chat.emptyChain') })
}

// ── 视觉能力（第十三轮）──
// 网关 /v1/models 会回 vision 字段（Agnes 3.0 Flash = true，其余文本模型默认 false）。
// 带图请求走路由别名时网关会把视觉模型排到链首；选了**不支持视觉的真实模型**时
// 这里提前告警并提供一键切换，避免"发了图但模型根本没看"。
const visionModels = computed(() =>
  textModels.value.filter((m) => m.vision).map((m) => m.id))
const visionOk = computed(() => {
  const m = routes.value.find((r) => r.id === params.model)
  if (m) return !!m.vision || !!(m.models || []).some((id: string) => visionModels.value.includes(id))
  const t2 = textModels.value.find((x) => x.id === params.model)
  return !!t2?.vision
})
const firstVisionModel = computed(() => visionModels.value[0] || '')
function useVisionModel() {
  if (firstVisionModel.value) params.model = firstVisionModel.value
}
function modelLabel(m: any) {
  return `${m.id} · ${m.provider_name}`
}

// ── 参数与状态 ──
const params = reactive({
  model: 'wjd',
  temperature: 0.7,
  top_p: 1,
  max_tokens: null as number | null,
  stream: true,
  system: '',
})
const paramsOpen = ref(false)
const streaming = ref(false)
const messages = ref<ChatMessage[]>([])
const text = ref('')
// 第十三轮: 视觉对话 —— 待发送消息附带的图片（上传的图片在浏览器内压成 data URI；也支持粘 URL）
interface PendingImg { uri: string; name: string; thumb: string }
const pending = ref<PendingImg[]>([])
const showImg = ref(false)
const imgUrl = ref('')
const fileEl = ref<HTMLInputElement | null>(null)
const MAX_CHAT_IMGS = 4

function pickImages() {
  fileEl.value?.click()
}
function resetFileInput() {
  if (fileEl.value) fileEl.value.value = ''
}
function basename(u: string): string {
  if (u.startsWith('data:')) return t('chat.uploadedImage')
  try { return decodeURIComponent((u.split('/').pop() || u)).slice(0, 24) } catch { return u.slice(0, 24) }
}
async function onFiles(ev: Event) {
  const input = ev.target as HTMLInputElement
  const files = Array.from(input.files || [])
  for (const f of files) {
    if (pending.value.length >= MAX_CHAT_IMGS) {
      ElMessage.warning(t('chat.imgMax', { n: MAX_CHAT_IMGS })); break
    }
    try {
      // 选图就不再只看 URL：浏览器内直接压缩成 data URI（上游实测可用）
      const m = await pickImage(f, PRESETS.chat)
      pending.value.push({ uri: m.uri, name: m.name, thumb: m.thumb })
    } catch (e: any) {
      ElMessage.error(t('chat.imgPickErr', { msg: (e && e.message) || e }))
    }
  }
  resetFileInput()
}
function addImg() {
  const u = (imgUrl.value || '').trim()
  if (u && !pending.value.some((x) => x.uri === u)) {
    pending.value.push({ uri: u, name: basename(u), thumb: '' })
  }
  imgUrl.value = ''
}
const scrollEl = ref<any>(null)
const userScrolled = ref(false)
let abortCtrl: AbortController | null = null

const SKEY = 'gw_chat_history'
// 写失败只提示一次，避免每次流式收尾都弹一遍
let warned = false

async function persist() {
  // 第十三轮: 上传的图片是 data URI（几百 KB），而服务端对话条目上限 512KB —— 直接存会被
  //   截断成坏 base64（图片显示不出来、后续带图请求还会报错）。所以落历史时**只存缩略图**，
  //   原图仅保留在本次会话内存里（发送用的仍是原图）。URL 图片不受影响，原样保存。
  const slim = messages.value.map((m) => ({
    id: m.id, role: m.role, content: m.content,
    images: (m.images || []).map((u, i) => (u.startsWith('data:') && m.imgThumbs?.[i] ? m.imgThumbs[i] : u)),
    imgNames: m.imgNames || [],
    reasoning: m.reasoning || '', meta: m.meta || null, usage: m.usage || null,
    cost: m.cost ?? null, error: m.error || '',
  }))
  const state = { params: { ...params, model: params.model }, messages: slim }
  try {
    sessionStorage.setItem(SKEY, JSON.stringify(state))   // 本地留一份当缓存/离线兜底
  } catch { /* 配额满就只靠服务端 */ }
  // 对话原先只存 sessionStorage，关标签页即丢；现在同步到服务端（按账号）
  if (!(await pushHistory('chat', [{ id: 'current', ...state }]))) {
    if (!warned) { warned = true; ElMessage.warning(t('history.saveFail')) }
  }
}
function applyDefaults(defs: any) {
  if (defs.model) params.model = defs.model
  if (typeof defs.temperature === 'number') params.temperature = defs.temperature
  if (typeof defs.top_p === 'number') params.top_p = defs.top_p
  if (defs.max_tokens != null) params.max_tokens = defs.max_tokens
  if (typeof defs.stream === 'boolean') params.stream = defs.stream
  if (defs.system != null) params.system = defs.system
}

function applyChat(j: any) {
  if (j?.params?.model) Object.assign(params, j.params)
  if (Array.isArray(j?.messages)) {
    let maxId = 0
    for (const m of j.messages) if (m.id > maxId) maxId = m.id
    seekId(maxId)
    messages.value = j.messages.map((m: any) => ({
      id: nextId(), role: m.role, content: m.content || '', images: m.images && m.images.length ? m.images : undefined,
      // 历史里的 data URI 已是被缩小的缩略图（见 persist），回填到 imgThumbs 让再次保存/展示一致
      imgNames: m.imgNames || [],
      imgThumbs: (m.images || []).map((u: string) => (u && u.startsWith('data:') ? u : '')),
      reasoning: m.reasoning || '', meta: m.meta || null,
      usage: m.usage || null, cost: m.cost ?? null, error: m.error || '',
      streaming: false,
    }))
  }
}

async function restore() {
  // M5: 无会话历史时，用设置页保存的默认参数做初始值。
  const defs = getChatDefaults()
  const remote = await pullHistory('chat')
  if (remote && remote.length) {
    applyChat(remote.find((x: any) => x.id === 'current') || remote[0])
    return
  }
  let raw: string | null = null
  try { raw = sessionStorage.getItem(SKEY) } catch { raw = null }
  if (!raw) { applyDefaults(defs); return }
  try {
    const j = JSON.parse(raw)
    applyChat(j)
    // 首次：把浏览器里的旧对话搬上服务端，之后就跟设备无关了
    if (remote) await pushHistory('chat', [{ id: 'current', ...j }])
  } catch { applyDefaults(defs) }
}

const tokensText = (u: any) =>
  [u.prompt_tokens != null ? `↑${u.prompt_tokens}` : '', u.completion_tokens != null ? `↓${u.completion_tokens}` : '']
    .filter(Boolean).join(' ') || t('chat.totalTokens', { n: u.total_tokens ?? 0 })

function scrollToBottom() {
  if (userScrolled.value) return
  // 第十九轮：必须等 DOM 更新完再滚。watch 默认 flush:'pre'，回调跑在渲染之前，
  // 读到的 scrollHeight 还是旧的 —— 打开长会话时会停在第 1 条（要一路往下拉）。
  nextTick(() => {
    const el = scrollEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}
function onScroll() {
  const el = scrollEl.value
  if (!el) return
  userScrolled.value = el.scrollHeight - el.scrollTop - el.clientHeight > 60
}

function onKeydown(e: KeyboardEvent) {
  // 输入法组词中的回车**不算**「发送」：中文 IME 确认候选词、手机软键盘的「换行/完成」
  // 都会抛 Enter keydown，此前会把半截话直接发出去。
  // keyCode 229 = 老浏览器/安卓键盘没有 isComposing 时的等价信号。Shift+Enter 仍是换行。
  const composing = e.isComposing || e.keyCode === 229
  if (e.key === 'Enter' && !e.shiftKey && !composing) {
    e.preventDefault()
    send()
  }
}

/** iOS 软键盘不会压缩 100dvh，输入框可能被键盘盖住：等键盘弹起后再滚一次。 */
function onInputFocus() {
  setTimeout(() => {
    const el = document.activeElement as HTMLElement | null
    el?.scrollIntoView({ block: 'nearest' })
  }, 320)
}

// ── 展示用：本条对话的时间 / 耗时 / token 合计 ──
// 只在组件内记（stamps 不落历史、不改接口也不进会话存储），刷新后历史消息没有时间戳，
// 对应字段就不显示，而不是编一个值。
const stamps = ref<Record<number, { at: number; ms?: number }>>({})
function markStart(id: number) { stamps.value[id] = { at: Date.now() } }
function markEnd(id: number) {
  const s = stamps.value[id]
  if (s && s.ms == null) s.ms = Date.now() - s.at
}
const pad2 = (n: number) => String(n).padStart(2, '0')
function fmtClock(ms: number) {
  const d = new Date(ms)
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
}
function fmtStamp(ms: number) {
  const d = new Date(ms)
  const now = new Date()
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()
  return sameDay
    ? `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
    : `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}
function metaText(m: ChatMessage): string {
  const s = stamps.value[m.id]
  const parts: string[] = []
  if (s) parts.push(fmtClock(s.at))
  if (m.role === 'assistant') {
    if (m.meta?.served) parts.push(m.meta.served)
    if (s && s.ms != null) parts.push((s.ms / 1000).toFixed(1) + 's')
    if (m.usage) parts.push(tokensText(m.usage))
  }
  return parts.join(' · ')
}
const lastAt = computed(() => {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    const s = stamps.value[messages.value[i].id]
    if (s) return s.at
  }
  return 0
})
const convTitle = computed(() => {
  const u = messages.value.find((m) => m.role === 'user' && m.content.trim())
  const s = (u?.content || '').replace(/\s+/g, ' ').trim()
  return s ? (s.length > 18 ? s.slice(0, 18) + '…' : s) : t('chat.newChat')
})
// 与服务端 history/chat 里当前那条同 id（多会话尚未实现，列表暂时只有这一条）
const activeConvId = ref('current')
const conversations = computed(() => [{
  id: activeConvId.value,
  title: convTitle.value,
  n: messages.value.length,
  time: lastAt.value ? fmtStamp(lastAt.value) : t('common.none'),
}])
const totalTokens = computed(() => {
  let sum = 0
  let seen = false
  for (const m of messages.value) {
    const u = m.usage
    if (!u) continue
    const v = u.total_tokens ?? ((u.prompt_tokens || 0) + (u.completion_tokens || 0))
    if (typeof v === 'number') { sum += v; seen = true }
  }
  return seen ? String(sum) : '—'
})
const elapsedText = computed(() => {
  let ms = 0
  for (const m of messages.value) {
    const s = stamps.value[m.id]
    if (s && s.ms) ms += s.ms
  }
  return ms ? (ms / 1000).toFixed(1) + 's' : '—'
})

// ── 发送/流式 ──
function buildPayload() {
  const history = messages.value.map((m) => ({ role: m.role, content: m.content, images: m.images }))
  return {
    model: params.model,
    messages: buildPayloadMessages(params.system, history),
    temperature: params.temperature,
    top_p: params.top_p,
    max_tokens: params.max_tokens ?? undefined,
    stream: params.stream,
  }
}

async function send() {
  const input = text.value.trim()
  if ((!input && !pending.value.length) || streaming.value) return
  const picked = pending.value.slice()
  // 整包体积兜底：素材（data URI）+ 正文必须落在网关请求体上限内，否则会拿 413
  try {
    assertBodyBudget(picked.map((p) => p.uri), input.length + 512)
  } catch (e: any) {
    ElMessage.error((e && e.message) || String(e))
    return
  }
  const userMsg: ChatMessage = {
    id: nextId(), role: 'user', content: input,
    images: picked.length ? picked.map((p) => p.uri) : undefined,
    imgNames: picked.length ? picked.map((p) => p.name) : undefined,
    imgThumbs: picked.length ? picked.map((p) => p.thumb || p.uri) : undefined,
  }
  markStart(userMsg.id)
  messages.value.push(userMsg)
  text.value = ''
  pending.value = []
  showImg.value = false
  imgUrl.value = ''
  userScrolled.value = false
  scrollToBottom()

  if (params.stream) await runStream(input)
  else await runOnce(input)
}

async function runOnce(input: string) {
  const reply: ChatMessage = { id: nextId(), role: 'assistant', content: '', streaming: true }
  markStart(reply.id)
  messages.value.push(reply)
  persist()
  try {
    const j = await chatOnce(buildPayload())
    if (j && j.choices && j.choices[0]) {
      reply.content = (j.choices[0].message && j.choices[0].message.content) || ''
    } else {
      reply.content = j?.content || ''
    }
    reply.usage = j?.usage || undefined
    reply.cost = j?.estimated_cost_cny ?? undefined
    if (j?.route || j?.provider) {
      reply.meta = {
        served: j.served_model || j.model || params.model,
        provider: j.provider_name || j.provider,
        requested: j.model || params.model,
        fallback: !!(j.route && j.route.fallback_used),
      }
    }
  } catch (e: any) {
    reply.error = e?.message || String(e)
  } finally {
    reply.streaming = false
    markEnd(reply.id)
    persist(); scrollToBottom()
  }
}

async function runStream(_input: string) {
  const reply: ChatMessage = { id: nextId(), role: 'assistant', content: '', reasoning: '', streaming: true }
  markStart(reply.id)
  messages.value.push(reply)
  streaming.value = true
  abortCtrl = new AbortController()
  persist(); scrollToBottom()

  const handle = (chunk: any) => {
    if (chunk.error) { reply.error = chunk.error; return }
    // 网关元信息帧（首次出现 served/provider 时记录）
    if (chunk.served_model || chunk.provider) {
      reply.meta = {
        served: chunk.served_model || chunk.model || params.model,
        provider: chunk.provider_name || chunk.provider,
        requested: chunk.model || params.model,
        fallback: !!(chunk.route && chunk.route.fallback_used),
      }
    }
    if (chunk.usage && !chunk.choices?.length) {
      reply.usage = chunk.usage
      if (chunk.estimated_cost_cny != null) reply.cost = chunk.estimated_cost_cny
    }
    const delta = chunk.choices && chunk.choices[0] && chunk.choices[0].delta
    if (delta) {
      if (delta.reasoning_content) reply.reasoning = (reply.reasoning || '') + delta.reasoning_content
      if (delta.content != null) reply.content += delta.content
    }
    scrollToBottom()
  }

  const fin = () => { reply.streaming = false; streaming.value = false; markEnd(reply.id); persist(); scrollToBottom() }

  try {
    await streamChat(buildPayload(), handle, abortCtrl.signal)
  } catch (e: any) {
    if (e && e.name === 'AbortError') {
      reply.error = reply.error || t('chat.stopped')
    } else {
      reply.error = e?.message || String(e)
    }
  } finally {
    fin()
  }
}

function stop() {
  if (abortCtrl) abortCtrl.abort()
}

function clearConversation() {
  messages.value = []
  persist()
}

// ── 重跑 / 复制 / 导出（都走已有数据与已有接口，不新增接口也不改存储键）──
const lastUser = computed(() => {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    if (messages.value[i].role === 'user') return messages.value[i]
  }
  return null
})

async function regenerate() {
  const u = lastUser.value
  if (!u || streaming.value) return
  // 丢掉最后一条用户消息之后的内容，再用同一段输入重跑（与 send() 同一条路径、同一份参数）
  const idx = messages.value.lastIndexOf(u)
  messages.value = messages.value.slice(0, idx)
  userScrolled.value = false
  scrollToBottom()
  if (params.stream) await runStream(u.content)
  else await runOnce(u.content)
}

async function copyAll() {
  const body = messages.value.map((m) => `[${m.role}] ${m.content}`).join('\n\n')
  try {
    await navigator.clipboard.writeText(body)
    ElMessage.success(t('common.copyOk'))
  } catch { ElMessage.warning(t('common.copyFail')) }
}

function exportConv() {
  const body = messages.value
    .map((m) => `## ${m.role}\n\n${m.content}${m.reasoning ? `\n\n<details>${t('chat.reasoningTag')}</details>\n\n${m.reasoning}` : ''}`)
    .join('\n\n')
  const href = URL.createObjectURL(new Blob([body], { type: 'text/markdown;charset=utf-8' }))
  const a = document.createElement('a')
  a.href = href
  a.download = `chat-${stamp()}.md`
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
  // 立刻 revoke 会打断部分浏览器尚未开始的保存，延后回收
  setTimeout(() => URL.revokeObjectURL(href), 10_000)
}

onMounted(() => {
  restore()
  loadModels()
})
onUnmounted(() => { if (abortCtrl) abortCtrl.abort() })

// 内容变化时自动滚动到底部（跟随流式输出）
watch(messages, scrollToBottom, { deep: true })
</script>

<style scoped>
/* ── 两栏骨架 ── */
/* 第十九轮：min-height:0 是关键 —— 不加它，flex/grid 子项的 min-height 默认取内容高度，
   里面的 .msgs（overflow-y:auto）就永远撑得开、滚不动，整页只能一路往下拉。 */
.chat-main { min-width: 0; display: flex; flex-direction: column; gap: var(--gap); height: 100%; min-height: 0; }

/* ── 左栏 ── */
.side-in { display: flex; flex-direction: column; gap: 8px; height: 100%; padding: 12px; }
.new-conv { width: 100%; }
.conv-list { display: flex; flex-direction: column; gap: 2px; overflow-y: auto; min-height: 0; }
.conv { cursor: pointer; }
.conv-info .nm { width: auto; color: var(--muted); }
.side-ops { margin-top: auto; display: flex; gap: 7px; }
.side-ops .op { flex: 1; }

/* ── 顶部参数胶囊 ── */
.chat-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.chat-top .chip-sel { cursor: pointer; }
.conv-sel { width: 170px; flex: none; }
.stream-badge { font-size: 12.5px; color: var(--warn); }
.vision-hint { margin-left: auto; font-size: 12px; color: var(--muted); }
.vision-hint.off { color: var(--warn); }

/* ── 消息卡 ── */
.chat-card { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.chat-card .msgs { flex: 1; min-height: 0; }
.empty { margin: auto; text-align: center; color: var(--muted); }
.empty-t { margin-top: 10px; font-size: 13px; }

.resp-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
.meta-txt { color: var(--muted); font-size: 12px; }
.reasoning { margin-bottom: 6px; border: 1px dashed var(--warn); border-radius: 6px; }
.reasoning :deep(.el-collapse-item__header) { font-size: 13px; color: var(--warn); background: transparent; }
.reasoning-md {
  white-space: pre-wrap; color: var(--muted); font-size: 12.5px;
  max-height: 240px; overflow-y: auto; background: var(--surface); padding: 6px; border-radius: 4px;
}
.attach :deep(.el-image) {
  width: 46px; height: 46px; border-radius: 8px; overflow: hidden;
  border: 1px solid var(--line); flex: none;
}
.plain { white-space: pre-wrap; }
.placeholder { color: var(--muted-2); font-style: italic; }
.cursor { display: inline-block; width: 2px; height: 16px; background: currentColor; vertical-align: text-bottom; animation: blink .8s infinite; }
@keyframes blink { 50% { opacity: 0; } }
.err { margin-top: 6px; }

.md :deep(pre) { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 10px; overflow-x: auto; font-size: 13px; }
.md :deep(code) { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.md :deep(h1), .md :deep(h2), .md :deep(h3) { margin: 8px 0; }
.md :deep(table) { border-collapse: collapse; }
.md :deep(td), .md :deep(th) { border: 1px solid var(--line); padding: 4px 8px; }

/* ── 输入区 ── */
.composer .in { padding-bottom: 8px; }
.composer .bar { flex-wrap: wrap; }
.img-bar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
.img-row { margin-bottom: 6px; }
.img-chip { max-width: 180px; }
.hide-file { display: none; }
.vision-warn { margin-bottom: 6px; }
.model-sel { flex: 1 1 150px; min-width: 110px; max-width: 230px; }
.opt { display: flex; align-items: center; gap: 6px; }
.tok { margin-left: auto; font-size: 11.5px; color: var(--muted-2); font-variant-numeric: tabular-nums; }

/* ── 参数抽屉 ── */
.param { margin-bottom: 18px; }
.plabel { font-size: 13px; color: var(--muted); margin-bottom: 6px; }

/* ── 窄屏 ── */
@media (max-width: 768px) {
  .vision-hint { margin-left: 0; width: 100%; }
  .model-sel { max-width: none; }
  /* 底部预留 iPhone 安全区（Home 指示条），否则「发送」按钮会被手势条压住一半 */
  .composer { padding-bottom: calc(10px + env(safe-area-inset-bottom, 0px)); }
}
</style>
