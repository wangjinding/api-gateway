<template>
  <div class="page-fill">
    <div class="img-layout">
      <!-- 左：参数（固定 320px；窄屏自动落到上方单列） -->
      <div class="card param-card">
        <div class="field mb-field">
          <label>{{ t('videos.model') }}</label>
          <el-select v-model="model" filterable class="fill">
            <el-option v-for="m in models" :key="m.id" :value="m.id" :label="`${m.id} · ${m.provider_name}`" />
          </el-select>
        </div>

        <!-- 智谱 CogVideoX-Flash / Agnes 两种视频契约：Agnes 用模式+时长+720P；智谱用 quality/fps/audio/像素 size -->
        <template v-if="!isZhipu">
          <!-- 第十一轮 #3：场景模式（对齐 Agnes Video 契约 text/keyframe/reference） -->
        <div class="field mb-field">
          <label>{{ t('videos.mode') }}</label>
          <div class="modes">
            <div class="mode" :class="{ on: mode === 'text' }" @click="mode = 'text'">
              {{ t('videos.modeText') }}<span class="mode-sub">text</span>
            </div>
            <div class="mode" :class="{ on: mode === 'keyframe' }" @click="mode = 'keyframe'">
              {{ t('videos.modeKeyframe') }}<span class="mode-sub">keyframe</span>
            </div>
            <div class="mode" :class="{ on: mode === 'reference' }" @click="mode = 'reference'">
              {{ t('videos.modeReference') }}<span class="mode-sub">reference</span>
            </div>
          </div>
        </div>

        <div class="form-row mb-field">
          <div class="field">
            <label>{{ t('videos.duration') }}</label>
            <!-- Agnes Video 2.5 Flash 文档: seconds 支持 "4"~"12"（3s 会被拒），默认 5 -->
            <el-select v-model="seconds" class="fill">
              <el-option v-for="s in durationOptions" :key="s" :value="String(s)" :label="`${s}s`" />
            </el-select>
          </div>
          <div class="field">
            <label>{{ t('videos.quality') }}</label>
            <!-- Flash 固定 720P（文档「size 仅支持字符串 720P」，其它值 HTTP 400）—— 只读展示 -->
            <div class="ctl dim">720P<span class="dim-tag">{{ t('videos.fixed') }}</span></div>
          </div>
        </div>

        <div class="form-row mb-field">
          <div class="field">
            <label>{{ t('videos.aspect') }}</label>
            <el-select v-model="aspect" class="fill">
              <el-option v-for="a in ['21:9', '16:9', '4:3', '1:1', '3:4', '9:16']" :key="a" :value="a" :label="a" />
            </el-select>
          </div>
          <div class="field">
            <label>{{ t('videos.seed') }}</label>
            <el-input-number v-model="seed" :min="0" :max="4294967295" :controls="false"
              :placeholder="t('videos.seedPH')" class="fill" />
          </div>
        </div>
        </template>
        <!-- 智谱 CogVideoX-Flash：文生视频专用（quality/fps/with_audio/像素 size，固定 text 模式） -->
        <template v-else>
          <div class="form-row mb-field">
            <div class="field">
              <label>{{ t('videos.zSize') }}</label>
              <el-select v-model="zsize" class="fill">
                <el-option v-for="s in zhipuSizes" :key="s" :value="s" :label="s" />
              </el-select>
            </div>
            <div class="field">
              <label>{{ t('videos.zQuality') }}</label>
              <el-select v-model="zquality" class="fill">
                <el-option value="speed" :label="t('videos.zSpeed')" />
                <el-option value="quality" :label="t('videos.zQualityHigh')" />
              </el-select>
            </div>
          </div>
          <div class="form-row mb-field">
            <div class="field">
              <label>{{ t('videos.zFps') }}</label>
              <el-select v-model="zfps" class="fill">
                <el-option :value="30" label="30fps" />
                <el-option :value="60" label="60fps" />
              </el-select>
            </div>
            <div class="field">
              <label>{{ t('videos.zAudio') }}</label>
              <el-switch v-model="zaudio" />
            </div>
          </div>
          <div class="hint tip">{{ t('videos.zTip') }}</div>
        </template>

        <div class="field mb-field">
          <label>{{ t('videos.promptRow') }}</label>
          <el-input v-model="prompt" type="textarea" :rows="3" resize="none" :placeholder="promptPlaceholder" :disabled="creating" />
        </div>

        <!-- 第十三轮：首尾帧 / 参考素材 —— 支持直接上传（浏览器内压缩成 data URI），也支持粘 URL -->
        <div v-if="mode === 'keyframe'" class="field mb-field">
          <label>{{ t('videos.modeKeyframe') }}</label>
          <input ref="frameEl" type="file" accept="image/*" class="hide-file" @change="onFrameFiles" />

          <div class="slot-row">
            <span class="slot-lb">{{ t('videos.firstFrame') }}</span>
            <el-button size="small" :disabled="creating" @click="pickFrame('first')">
              <Icon name="up" :size="14" />{{ t('videos.upload') }}
            </el-button>
            <el-input v-model="firstUrl" size="small" :placeholder="t('videos.urlPH')" clearable class="slot-u" />
          </div>
          <div v-if="firstSlot" class="slot-file" :title="firstSlot.name">
            <img :src="firstSlot.thumb || firstSlot.uri" alt="" />
            <span class="slot-name">{{ firstSlot.name }}</span>
            <span class="rm" @click="firstSlot = null"><Icon name="x" :size="11" /></span>
          </div>

          <div class="slot-row">
            <span class="slot-lb">{{ t('videos.lastFrame') }}</span>
            <el-button size="small" :disabled="creating" @click="pickFrame('last')">
              <Icon name="up" :size="14" />{{ t('videos.upload') }}
            </el-button>
            <el-input v-model="lastUrl" size="small" :placeholder="t('videos.urlPH')" clearable class="slot-u" />
          </div>
          <div v-if="lastSlot" class="slot-file" :title="lastSlot.name">
            <img :src="lastSlot.thumb || lastSlot.uri" alt="" />
            <span class="slot-name">{{ lastSlot.name }}</span>
            <span class="rm" @click="lastSlot = null"><Icon name="x" :size="11" /></span>
          </div>

          <div class="hint tip">{{ t('videos.keyframeTip') }}</div>
        </div>

        <div v-else-if="mode === 'reference'" class="field mb-field">
          <label>{{ t('videos.modeReference') }}</label>
          <input ref="refImgEl" type="file" accept="image/*" multiple class="hide-file" @change="onRefImages" />
          <input ref="refAudEl" type="file" :accept="audioAccept" multiple class="hide-file" @change="onRefAudios" />

          <div class="ref-btns">
            <el-button size="small" :disabled="creating" @click="refImgEl?.click()">
              <Icon name="up" :size="14" />{{ t('videos.uploadImg') }}
            </el-button>
            <el-button size="small" @click="showRefImgUrl = !showRefImgUrl">
              <Icon name="link" :size="14" />{{ t('videos.useUrl') }}
            </el-button>
            <span class="hint nowrap">{{ t('videos.count5', { n: refImgs.length }) }}</span>
          </div>
          <div v-if="refImgs.length" class="ref-thumbs">
            <div v-for="(r, i) in refImgs" :key="i" class="ref-thumb" :title="`${r.name} · ${formatBytes(dataUriBytes(r.uri))}`">
              <img :src="r.thumb || r.uri" alt="" />
              <span class="rm" @click="refImgs.splice(i, 1)"><Icon name="x" :size="11" /></span>
              <span class="rn">{{ i + 1 }}. {{ r.name }}</span>
            </div>
          </div>
          <el-input v-if="showRefImgUrl" v-model="refImgUrls" type="textarea" :rows="2" resize="none"
            :placeholder="t('videos.refImagesPH')" :disabled="creating" class="ref-urls" />

          <div class="ref-btns mt6">
            <el-button size="small" :disabled="creating" @click="refAudEl?.click()">
              <Icon name="up" :size="14" />{{ t('videos.uploadAudio') }}
            </el-button>
            <el-button size="small" @click="showRefAudUrl = !showRefAudUrl">
              <Icon name="link" :size="14" />{{ t('videos.useUrl') }}
            </el-button>
            <span class="hint nowrap">{{ t('videos.count3', { n: refAuds.length }) }}</span>
          </div>
          <div v-if="refAuds.length" class="ref-thumbs">
            <div v-for="(a, i) in refAuds" :key="i" class="ref-aud" :title="a.name">
              <Icon name="clip" :size="14" class="au-ico" />
              <span class="rn">{{ i + 1 }}. {{ a.name }} · {{ formatBytes(a.bytes) }}</span>
              <span class="rm" @click="refAuds.splice(i, 1)"><Icon name="x" :size="11" /></span>
            </div>
          </div>
          <el-input v-if="showRefAudUrl" v-model="refAudUrls" type="textarea" :rows="2" resize="none"
            :placeholder="t('videos.refAudiosPH')" :disabled="creating" class="ref-urls" />

          <div class="hint tip">{{ t('videos.referenceTip') }}</div>
          <div class="hint tip">{{ t('videos.uploadTip') }}</div>
        </div>

        <div class="field async-row">
          <el-checkbox v-model="stream" :disabled="creating">{{ t('videos.async') }}</el-checkbox>
        </div>

        <div class="param-foot">
          <el-alert v-if="err" :title="err" type="error" show-icon :closable="false" class="err-alert" />
          <el-button type="primary" class="go-btn" :loading="creating" :disabled="!prompt.trim()" @click="create">
            <Icon name="vid" :size="15" />{{ t('videos.generate') }}
          </el-button>
        </div>
      </div>

      <!-- 右：任务列表（卡片自己撑满，滚动只发生在任务区） -->
      <div class="card result-card">
        <div class="card-h">
          <b>{{ t('videos.taskTitle') }}</b>
          <span class="sub">{{ t('videos.taskCount', { n: tasks.length }) }}</span>
          <div class="pager">
            <span class="chip-sel" :class="{ on: filter === 'all' }" @click="filter = 'all'">{{ t('common.all') }}</span>
            <span class="chip-sel" :class="{ on: filter === 'doing' }" @click="filter = 'doing'">{{ t('videos.doingText') }}</span>
            <span class="chip-sel" :class="{ on: filter === 'done' }" @click="filter = 'done'">{{ t('videos.doneText') }}</span>
            <el-button size="small" class="nowrap" :disabled="!doneCount" @click="clearDone">{{ t('videos.clear') }}</el-button>
          </div>
        </div>

        <div class="task-list">
          <!-- 第十九轮版式：左 16:9 缩略图 + 右三行紧凑信息（标题/状态 · 摘要行 · 任务号+操作）。
               原来是「小缩略图 + 5 行 key/value + 折叠栏 + 单占一行的按钮」，卡片被撑到 228px，
               缩略图下面空出 140px，看着散；状态还在胶囊（上游英文 completed）和正文（已生成）里说了两遍。 -->
          <div v-for="tk in shownTasks" :key="tk.id" class="vid-card" :class="'k-' + tk.kind">
            <div class="vid-thumb">
              <video v-if="tk.src" :src="tk.src" controls preload="metadata" class="vid-el" :poster="tk.poster || undefined"></video>
              <span v-else-if="tk.kind === 'pending'" class="spinner"></span>
              <Icon v-else :name="tk.kind === 'fail' ? 'x' : 'play'" :size="22" />
            </div>

            <div class="vid-body">
              <div class="vid-head">
                <b class="t" :title="tk.prompt">{{ tk.prompt || tk.video_id }}</b>
                <span class="pill" :class="pillCls(tk.kind)">{{ stateShort(tk) }}</span>
              </div>

              <!-- 摘要行：服务 · 耗时/等待 · 场景 挤成一行；失败时这里就是失败原因（原来藏在折叠里） -->
              <div class="s" :class="{ 's-err': tk.kind === 'fail' }" :title="tk.kind === 'fail' ? (tk.error || '') : ''">
                {{ subLine(tk) }}
              </div>
              <div v-if="tk.refs" class="s refs" :title="tk.refs">{{ tk.refs }}</div>

              <!-- 进度条只在「生成中」出现：已完成的满格绿条 / 失败的红条像一条卡片分隔线，纯噪声 -->
              <div v-if="tk.kind === 'pending'" class="progress"><i class="bar-wait" /></div>

              <div class="vid-foot">
                <span class="vid-k">{{ t('videos.task') }}</span>
                <span class="vid-id code" :title="tk.video_id">{{ tk.video_id }}</span>
                <div class="acts">
                  <!-- 第十七轮: 下载。产物在跨域 CDN（不带 CORS 头）上，直链 <a download> 不生效，
                       统一走后端 /v1/media/download 代拉，见 lib/download.ts。 -->
                  <el-button v-if="tk.src" size="small" :loading="dlId === (tk.id ?? tk.video_id)" @click="dlVideo(tk)">
                    <Icon name="dl" :size="14" />{{ t('videos.dl') }}
                  </el-button>
                  <!-- 「原始返回」改成内联按钮（原 el-collapse 独占一行还多一道边框），展开就在卡片底部 -->
                  <el-button v-if="tk.error || tk.rawtext" size="small" text @click="toggleRaw(tk.id)">
                    {{ t('videos.raw') }}<Icon :name="rawOpen(tk.id) ? 'chevD' : 'chev'" :size="14" />
                  </el-button>
                </div>
              </div>

              <pre v-if="rawOpen(tk.id)" class="raw-text">{{ tk.error || tk.rawtext }}</pre>
            </div>
          </div>

          <el-empty v-if="!shownTasks.length" :description="t('videos.empty')" />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import Icon from '../components/Icon.vue'
import { api } from '../api'
import { findVideoUrl, videoStatusKind } from '../lib/media'
import { downloadMedia, nameFromPrompt, stamp } from '../lib/download'
import { pullHistory, pushHistory, deleteHistoryItem, ensureIds } from '../lib/history'
import { pickImage, pickAudio, PRESETS, dataUriBytes, formatBytes, assertBodyBudget } from '../lib/upload'

interface VTask {
  id: number
  video_id: string
  model: string
  provider: string
  status: string
  kind: 'pending' | 'done' | 'fail'
  src: string
  poster: string
  started: number
  prompt?: string
  elapsed?: number
  mode?: string
  refs?: string          // 素材摘要（文件名，不放 data URI —— 历史单条只有 16KB）
  retryAt?: number       // 上游轮询被限流时，到这个时间点前不再问（避免撞 429）
  error?: string
  rawtext: string
}

interface Slot { uri: string; name: string; thumb: string; bytes: number }

const router = useRouter()
const { t } = useI18n()
const models = ref<any[]>([])
const model = ref('')
// 所选视频模型是否智谱 CogVideoX（文生视频专用表单）
const isZhipu = computed(() => models.value.find((m: any) => m.id === model.value)?.provider === 'bigmodel')
// 切到智谱视频模型时强制 text 文生视频模式（智谱无 keyframe/reference）
watch(model, (m) => {
  const mo = models.value.find((x: any) => x.id === m)
  if (mo?.provider === 'bigmodel') mode.value = 'text'
})
const prompt = ref('')
const seconds = ref('5')
const aspect = ref('16:9')
const quality = ref('720P')
const seed = ref<number | null>(null)
// 第十一轮 #3：场景模式与对应素材
const mode = ref<'text' | 'keyframe' | 'reference'>('text')
// 智谱 CogVideoX-Flash：text 文生视频专用参数（quality/fps/with_audio/像素 size）
const zhipuSizes = ['720x480', '1024x1024', '1280x960', '960x1280', '1920x1080', '1080x1920', '2048x1080']
const zsize = ref('1920x1080')
const zquality = ref<'speed' | 'quality'>('speed')
const zfps = ref(30)
const zaudio = ref(true)
const frameEl = ref<HTMLInputElement | null>(null)
const refImgEl = ref<HTMLInputElement | null>(null)
const refAudEl = ref<HTMLInputElement | null>(null)
let frameTarget: 'first' | 'last' = 'first'
const firstSlot = ref<Slot | null>(null)
const lastSlot = ref<Slot | null>(null)
const firstUrl = ref('')
const lastUrl = ref('')
const refImgs = ref<Slot[]>([])        // 参考图（最多 5，文档 images ≤ 5）
const refAuds = ref<Slot[]>([])        // 参考音频（最多 3，文档 audios ≤ 3）
const refImgUrls = ref('')             // 每行一个 URL / data:
const refAudUrls = ref('')
const showRefImgUrl = ref(false)
const showRefAudUrl = ref(false)
const audioAccept = 'audio/*,.mp3,.wav,.m4a,.aac,.ogg,.flac,.opus'
const durationOptions = [4, 5, 6, 7, 8, 9, 10, 11, 12]
const stream = ref(true)     // 异步提交，后台轮询
const creating = ref(false)
const err = ref('')
const tasks = ref<VTask[]>([])
// 已完成（含失败）的任务数：决定「清除已完成」是否可点 —— 没东西可清时按钮直接置灰，
// 免得点了弹一个「确定要清除 0 个吗」的框。
const doneCount = computed(() => tasks.value.filter((t) => t.kind !== 'pending').length)

// 任务列表筛选（纯展示：轮询与统计仍然针对全部任务，不裁剪原数组）
const filter = ref<'all' | 'doing' | 'done'>('all')
const shownTasks = computed(() => tasks.value.filter((tk) =>
  filter.value === 'all' ? true : filter.value === 'doing' ? tk.kind === 'pending' : tk.kind !== 'pending'))

// 把多行文本切成非空 URL 数组
function toList(s: string): string[] {
  return (s || '').split(/\r?\n/).map((x) => x.trim()).filter(Boolean)
}
const promptPlaceholder = computed(() =>
  mode.value === 'reference' ? t('videos.placeholderRef') : t('videos.placeholder'))

const SKEY = 'gw_video_tasks'   // 本地只当缓存，历史以服务端为准

function cacheLocal() {
  try { localStorage.setItem(SKEY, JSON.stringify(tasks.value)) } catch { /* 缓存写不进无所谓 */ }
}
async function save() {
  cacheLocal()
  // 视频任务原先因为 rawtext 太大把 localStorage 配额撑满、写失败又被静默吞掉 —— 这里必须报出来
  if (!(await pushHistory('videos', tasks.value))) ElMessage.warning(t('history.saveFail'))
}
async function load() {
  let local: VTask[] = []
  try {
    const j = JSON.parse(localStorage.getItem(SKEY) || '[]')
    if (Array.isArray(j)) local = j as VTask[]
  } catch { local = [] }
  const remote = await pullHistory('videos')
  if (remote === null) {
    tasks.value = local.map((t) => ({ ...t, kind: videoStatusKind(t.status) }))
    ElMessage.warning(t('history.offline'))
    return
  }
  if (remote.length) {
    tasks.value = remote.map((t: any) => ({ ...t, kind: videoStatusKind(t.status) }))
  } else if (local.length) {
    tasks.value = ensureIds(local, 'vid').map((t: any) => ({ ...t, kind: videoStatusKind(t.status) }))
    await pushHistory('videos', tasks.value)          // 首次：把浏览器里的旧任务搬上服务端
  } else {
    tasks.value = []
  }
  cacheLocal()
}

async function loadModels() {
  try {
    const j = await api('/v1/models')
    const all = (j.data || []).filter((x: any) => x.type === 'video')
    models.value = all
    if (all.length && !model.value) model.value = all[0].id
  } catch (e: any) {
    if (e && e.status === 401) { router.push('/login'); return }
    ElMessage.error(t('videos.loadErr') + '：' + ((e && e.message) || e))
  }
}

// 卡片状态文案。注意参数别叫 t —— 那会遮蔽 i18n 的 t()，videos.failText 分支会当场抛错。
// 第十九轮：胶囊里只放短状态（原来直接显示上游的 completed / failed 英文原样，
// 而正文里又写一遍「已生成」，同一件事说两遍；失败原因挪到摘要行，仍可见）
const stateShort = (tk: VTask) =>
  tk.kind === 'fail' ? t('videos.failText') : tk.kind === 'done' ? t('videos.doneText') : t('videos.doingText')

// 「生成中」已等待了多久（started 是毫秒时间戳；轮询每次刷新会重算）
const waitSec = (tk: VTask) => (tk.started ? Math.max(0, Math.round((Date.now() - tk.started) / 1000)) : 0)

// 摘要行：服务 · 耗时/等待 · 场景，一行说完；失败时整行就是失败原因
function subLine(tk: VTask): string {
  if (tk.kind === 'fail') return tk.error || t('videos.failText')
  const parts: string[] = []
  if (tk.provider) parts.push(tk.provider)
  if (tk.kind === 'done' && tk.elapsed) parts.push(t('videos.subElapsed', { n: tk.elapsed }))
  if (tk.kind === 'pending') { const w = waitSec(tk); if (w) parts.push(t('videos.subWait', { n: w })) }
  if (tk.mode) parts.push(modeLabel(tk.mode))
  return parts.join(' · ')
}

// 「原始返回」的展开态（内联按钮切换，替代原来的 el-collapse）
const rawIds = ref<number[]>([])
const rawOpen = (id: number) => rawIds.value.includes(id)
function toggleRaw(id: number) {
  rawIds.value = rawIds.value.includes(id) ? rawIds.value.filter((x) => x !== id) : [...rawIds.value, id]
}

const pillCls = (k: string) => (k === 'done' ? 'ok' : k === 'fail' ? 'err' : 'warn')

const modeLabel = (m: string) =>
  m === 'keyframe' ? t('videos.modeKeyframe') : m === 'reference' ? t('videos.modeReference') : t('videos.modeText')

// ── 素材上传（第十三轮）──
function pickFrame(which: 'first' | 'last') {
  frameTarget = which
  frameEl.value?.click()
}
async function onFrameFiles(ev: Event) {
  const input = ev.target as HTMLInputElement
  const f = (input.files || [])[0]
  if (f) {
    try {
      // 首/尾帧会给到 1280px（画面直接进视频，比参考图要求更高）
      const m = await pickImage(f, PRESETS.frame)
      if (frameTarget === 'first') firstSlot.value = m
      else lastSlot.value = m
    } catch (e: any) {
      ElMessage.error(t('videos.pickErr', { msg: (e && e.message) || e }))
    }
  }
  if (input) input.value = ''
}
async function onRefImages(ev: Event) {
  const input = ev.target as HTMLInputElement
  for (const f of Array.from(input.files || [])) {
    if (refImgs.value.length >= 5) { ElMessage.warning(t('videos.refMax', { n: 5 })); break }
    try {
      refImgs.value.push(await pickImage(f, PRESETS.videoRef))
    } catch (e: any) {
      ElMessage.error(t('videos.pickErr', { msg: (e && e.message) || e }))
    }
  }
  if (input) input.value = ''
}
async function onRefAudios(ev: Event) {
  const input = ev.target as HTMLInputElement
  for (const f of Array.from(input.files || [])) {
    if (refAuds.value.length >= 3) { ElMessage.warning(t('videos.audMax', { n: 3 })); break }
    try {
      refAuds.value.push(await pickAudio(f))
    } catch (e: any) {
      ElMessage.error(t('videos.pickErr', { msg: (e && e.message) || e }))
    }
  }
  if (input) input.value = ''
}
/** 上传的 data URI 与手填 URL 合成上游字段（首/尾帧取其一：上传优先） */
const firstFrame = computed(() => (firstSlot.value?.uri || firstUrl.value).trim())
const lastFrame = computed(() => (lastSlot.value?.uri || lastUrl.value).trim())
const imageList = computed(() => [...refImgs.value.map((r) => r.uri), ...toList(refImgUrls.value)])
const audioList = computed(() => [...refAuds.value.map((r) => r.uri), ...toList(refAudUrls.value)])
/** 卡片上展示的素材摘要（只存名字/数量，data URI 不进历史） */
function mediaSummary(): string {
  if (mode.value === 'keyframe') {
    const names: string[] = []
    if (firstSlot.value) names.push(`首帧:${firstSlot.value.name}`)
    else if (firstUrl.value.trim()) names.push('首帧:URL')
    if (lastSlot.value) names.push(`尾帧:${lastSlot.value.name}`)
    else if (lastUrl.value.trim()) names.push('尾帧:URL')
    return names.join(' / ')
  }
  if (mode.value === 'reference') {
    const names: string[] = []
    refImgs.value.forEach((r, i) => names.push(`图${i + 1}:${r.name}`))
    if (toList(refImgUrls.value).length) names.push(`图URL×${toList(refImgUrls.value).length}`)
    refAuds.value.forEach((r, i) => names.push(`音${i + 1}:${r.name}`))
    if (toList(refAudUrls.value).length) names.push(`音URL×${toList(refAudUrls.value).length}`)
    return names.join(' / ')
  }
  return ''
}

async function create() {
  if (!prompt.value.trim() || creating.value) return
  // 第十一轮 #3：按模式做前端轻校验（后端也会挡，这里提前给反馈）
  if (mode.value === 'keyframe' && !firstFrame.value && !lastFrame.value) {
    err.value = t('videos.needFrame'); return
  }
  if (mode.value === 'reference' && !imageList.value.length && !audioList.value.length) {
    err.value = t('videos.needRef'); return
  }
  // 整包体积（素材 data URI + 正文）必须落在网关请求体上限内
  try {
    assertBodyBudget([prompt.value, firstFrame.value, lastFrame.value, ...imageList.value, ...audioList.value], 1200)
  } catch (e: any) {
    err.value = (e && e.message) || String(e); return
  }
  creating.value = true
  err.value = ''
  const t0 = Date.now()
  const summary = mediaSummary()
  try {
    const body: any = { model: model.value, prompt: prompt.value }
    if (isZhipu.value) {
      // 智谱 CogVideoX-Flash：text 文生视频；quality/fps/with_audio 走 extra（后端透传给上游）
      body.size = zsize.value
      body.mode = 'text'
      body.extra = { quality: zquality.value, fps: zfps.value, with_audio: zaudio.value }
    } else {
      body.seconds = seconds.value
      body.size = quality.value
      body.aspect_ratio = aspect.value
      body.mode = mode.value
      if (seed.value != null) body.seed = seed.value
      if (mode.value === 'keyframe') {
        if (firstFrame.value) body.first_frame = firstFrame.value
        if (lastFrame.value) body.last_frame = lastFrame.value
      } else if (mode.value === 'reference') {
        if (imageList.value.length) body.images = imageList.value
        if (audioList.value.length) body.audios = audioList.value
      }
    }
    if (!stream.value) body.wait = true       // 同步等结果
    const j = await api('/v1/videos', { method: 'POST', body: JSON.stringify(body) })
    const task: VTask = {
      id: Date.now() + Math.random(), video_id: j.video_id || `unknown-${Date.now()}`,
      model: model.value, provider: j.provider_name || j.provider || '',
      status: j.status || 'submitted', kind: videoStatusKind(j.status || 'submitted'),
      src: '', poster: '', started: t0, rawtext: '',
      mode: mode.value,
      refs: summary || undefined,
      prompt: prompt.value.trim(),   // 卡片下面要能回看当时填的提示词
    }
    if (j.final) {
      // 同步等待已返回最终 raw
      task.src = findVideoUrl(j.final)
      task.rawtext = JSON.stringify(j.final, null, 2)
      if (task.src) task.kind = 'done'
    }
    tasks.value.unshift(task)
    save()
    if (task.kind === 'done') {
      ElMessage.success(t('videos.doneMsg'))
    } else if (stream.value) {
      ElMessage.success(t('videos.submitMsg', { id: task.video_id }))
    }
  } catch (e: any) {
    // 第九轮: 优先展示后端给的人话（如「上游拒绝请求：size 必须是 720P 之一」）
    err.value = (e?.body?.detail) || e?.message || String(e)
  } finally {
    creating.value = false
  }
}

// 轮询所有 pending 任务
// 间隔保持 5s（不按文档建议的 1~2s）：实测上游 /agnesapi 在 3s 一次的频率下就会
// 回 429「查询过于频繁」，网关已把 429 折成 throttled（不报错、不计熔断），
// 这里再退避一点，避免多个任务同时轮询把额度打满。
async function poll() {
  const now = Date.now()
  for (const tk of tasks.value) {
    if (tk.kind !== 'pending') continue
    if (!tk.video_id || tk.video_id.startsWith('unknown')) continue
    if (tk.retryAt && tk.retryAt > now) continue
    try {
      const j = await api(`/v1/videos/${encodeURIComponent(tk.video_id)}?model=${encodeURIComponent(tk.model)}`)
      if (j && j.throttled) {
        // 上游限流：任务还在跑，这次只是没问到 —— 不算失败，也别把卡片显示成出错
        const wait = Number(j.retry_after) || 3
        tk.retryAt = Date.now() + wait * 1000
        continue
      }
      const st = j.status || 'pending'
      tk.status = st
      const k = videoStatusKind(st)
      if (k === 'done') {
        tk.src = findVideoUrl(j.raw)
        tk.rawtext = JSON.stringify(j.raw, null, 2)
        tk.elapsed = ((Date.now() - tk.started) / 1000) | 0
      } else if (k === 'fail') {
        tk.error = JSON.stringify(j.raw || j).slice(0, 300)
      }
      if (tk.src) tk.kind = 'done'
      else if (k === 'fail') tk.kind = 'fail'
      save()
    } catch (e: any) {
      // 任务暂不存在/仍提交中：不打断轮询，仅记住错误供展示
      tk.error = (e && e.message) || String(e)
    }
  }
}

async function clearDone() {
  const gone = tasks.value.filter((t) => t.kind !== 'pending')
  if (!gone.length) return
  // 老板要求：清除已完成要二次确认 —— 这是服务端真删（不可逆），点错就没了。
  try {
    await ElMessageBox.confirm(t('videos.confirmClear', { n: gone.length }), t('common.confirm'),
      { type: 'warning', confirmButtonText: t('common.delete'), cancelButtonText: t('common.cancel') })
  } catch { return }
  tasks.value = tasks.value.filter((t) => t.kind === 'pending')
  cacheLocal()
  // 逐条按 id 删，别整份覆盖（覆盖会把另一台设备刚生成的也抹掉）
  for (const tk of gone) {
    if (!(await deleteHistoryItem('videos', tk.id))) ElMessage.warning(t('history.saveFail'))
  }
}

let timer: any = null
onMounted(() => {
  load()
  loadModels()
  timer = setInterval(poll, 5000)
})

// ── 第十七轮: 下载已生成的视频 ──
// CDN 不给 CORS 头，所以由后端代拉（/v1/media/download）再落盘；文件名取
// 「提示词前缀 + 时间戳」，后缀由后端按 Content-Type 补（默认 .mp4）。
const dlId = ref<any>(null)
async function dlVideo(tk: VTask) {
  if (!tk?.src) { ElMessage.error(t('videos.dlNoSrc')); return }
  dlId.value = tk.id ?? tk.video_id
  try {
    await downloadMedia(tk.src, `${nameFromPrompt(tk.prompt || '', 'video')}-${stamp()}`)
    ElMessage.success(t('videos.dlOk'))
  } catch (e: any) {
    ElMessage.error(t('videos.dlFail', { msg: e?.message || String(e) }))
  } finally {
    dlId.value = null
  }
}
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
/* 撑满高度的两栏：左参数卡、右任务卡；滚动只发生在各自内部 */
.param-card { display: flex; flex-direction: column; overflow-y: auto; }
.result-card { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.task-list { flex: 1; min-height: 0; overflow-y: auto; }

.mb-field { margin-bottom: 10px; }
/* .field 天生是 flex:1（给 .form-row 横向用的）—— 在竖排卡片里必须收回，
   否则每个字段会纵向撑开，控件之间留下大片空白 */
.param-card > .field { flex: none; }
.fill { width: 100%; }
.mt6 { margin-top: 8px; }
.mode-sub { display: block; font-size: 10px; line-height: 1.3; color: var(--muted-2); margin-top: 1px; }
.mode.on .mode-sub { color: var(--primary); opacity: .75; }
/* 分辨率只读展示：半透明 + 「固定」说明 */
.ctl.dim { opacity: .6; }
.dim-tag { margin-left: auto; font-size: 11px; }
.async-row { margin-top: 2px; }

.ref-btns { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
/* 「上传 / 用 URL」文案不折行（窄列里被挤成两行是旧版的老毛病） */
.ref-btns :deep(.el-button) { white-space: nowrap; }
.hide-file { display: none; }
.ref-urls { margin-top: 6px; }
.ref-thumbs { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
.ref-thumb { position: relative; width: 68px; }
.ref-thumb img { width: 68px; height: 68px; object-fit: cover; border-radius: 6px; border: 1px solid var(--line); display: block; }
.ref-thumb .rn { display: block; font-size: 11px; color: var(--muted-2); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ref-aud {
  position: relative; display: flex; align-items: center; gap: 6px; padding: 4px 22px 4px 8px;
  border: 1px solid var(--line); border-radius: 6px; background: var(--surface-2);
}
.ref-aud .au-ico { color: var(--muted-2); }
.ref-aud .rn { font-size: 12px; color: var(--muted); max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.rm {
  position: absolute; top: -6px; right: -6px; width: 18px; height: 18px;
  display: grid; place-items: center; border-radius: 50%; cursor: pointer;
  background: var(--surface); border: 1px solid var(--line); color: var(--err);
}
.ref-aud .rm { top: 50%; right: 2px; transform: translateY(-50%); }

.slot-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
.slot-lb { flex: 0 0 60px; font-size: 12px; color: var(--muted); }
.slot-u { flex: 1 1 150px; min-width: 140px; }
.slot-row :deep(.el-button) { white-space: nowrap; }
.slot-file {
  position: relative; display: inline-flex; align-items: center; gap: 6px; padding: 2px 6px;
  border: 1px solid var(--line); border-radius: 6px; background: var(--surface-2); margin-bottom: 8px;
}
.slot-file img { width: 30px; height: 30px; object-fit: cover; border-radius: 4px; display: block; }
.slot-name { font-size: 12px; color: var(--muted); max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tip { margin-top: 6px; line-height: 1.5; }

.param-foot { margin-top: auto; padding-top: 8px; }
.err-alert { margin-bottom: 8px; }
.go-btn { width: 100%; }

.pager { gap: 6px; flex-wrap: wrap; }
.chip-sel { cursor: pointer; }
.pager :deep(.el-button) { margin-left: 6px; }

/* 任务卡：缩略占位 + 右侧标题 / 状态 / 摘要 / 操作 */
/* 第十九轮：卡片改成「缩略图垂直居中 + 右侧三行信息」的紧凑版式。
   信息压到 3 行（原来 5 行 key/value + 折叠栏 + 独占一行的按钮 = 228px 高的卡片，
   缩略图下方空 140px）。 */
.vid-card { align-items: center; }
.vid-card.k-fail { border-color: color-mix(in srgb, var(--err) 35%, var(--line)); }
.vid-card .vid-thumb { overflow: hidden; }
.vid-el { width: 100%; height: 100%; object-fit: contain; background: var(--surface-2); }
.spinner {
  width: 22px; height: 22px; border: 3px solid var(--line); border-top-color: var(--primary);
  border-radius: 50%; animation: rot .8s linear infinite;
}
@keyframes rot { to { transform: rotate(360deg); } }
.vid-body { flex: 1; min-width: 0; }
.vid-head { display: flex; align-items: center; gap: 8px; }
.vid-head .t {
  font-size: 13px; font-weight: 600; color: var(--text); min-width: 0;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.vid-head .pill { margin-left: auto; }
/* 摘要行：一行放不下就省略（原来换成 5 行 key/value，卡片高度翻倍） */
.vid-card .s {
  font-size: 11.5px; color: var(--muted-2); margin-top: 3px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.vid-card .s.refs { color: var(--muted); }
.vid-card .s.s-err { color: var(--err); }
.progress { height: 5px; }
.bar-wait { width: 38%; background: var(--primary); animation: wait 1.4s ease-in-out infinite; }@keyframes wait { 0%, 100% { opacity: .35 } 50% { opacity: 1 } }
/* 底部一行：任务号 + 操作按钮；窄屏放不下时按钮自动换到下一行 */
.vid-foot { display: flex; align-items: center; gap: 8px; margin-top: 7px; flex-wrap: wrap; }
.vid-k { color: var(--muted-2); flex: none; font-size: 12px; }
.vid-id { color: var(--muted); flex: 1 1 auto; min-width: 90px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.raw-text {
  white-space: pre-wrap; font-size: 11px; color: var(--muted); max-height: 160px; overflow-y: auto;
  background: var(--surface-2); padding: 6px; border-radius: 4px; border: 1px solid var(--line);
  margin: 7px 0 0;
}
.acts { display: flex; flex-wrap: wrap; gap: 7px; margin-left: auto; }
.acts :deep(.el-button) { margin-left: 0; }

/* 窄屏：单列后整页滚动，两个卡片都恢复自然高度 */
@media (max-width: 768px) {
  /* 窄屏单列后不再需要「撑满一屏」，改由 .gw-body 整页滚动 */
  .page-fill { height: auto; }
  .img-layout { height: auto; }
  .param-card { overflow: visible; }
  .result-card { flex: none; }
  .task-list { overflow: visible; }
  .card-h { flex-wrap: wrap; }
  .slot-lb { flex: 0 0 52px; }
  /* 第十八轮: 场景卡副标题原 10px，手机上太小（与底部 Tab 一起抬到 11.5px） */
  .mode-sub { font-size: 11.5px; }
  /* 第十九轮：窄屏下按钮会换到第二行，卡片本来就比 61px 的缩略图高一截，
     缩略图改为撑满卡片高度 + cover 裁切（与图像页缩略图 fit=cover 一致），
     左侧不再悬着一张 61px 高的小图、上下各空 45px。 */
  .vid-card .vid-thumb { align-self: stretch; height: auto; min-height: 61px; }
  .vid-el { object-fit: cover; }
  /* 场景卡的两列布局已上移到 style.css 的 .modes（原为「窄屏两列、桌面三列」，
     改后桌面也是两列 —— 桌面参数列只有 ~289px，三列每格 91px 放不下长标签）。 */
}
</style>
