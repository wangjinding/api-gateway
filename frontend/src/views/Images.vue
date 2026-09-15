<template>
  <div class="page-fill">
    <div class="img-layout">
      <!-- 左：参数（固定 320px；窄屏自动落到上方单列） -->
      <div class="card param-card">
        <div class="field mb-field">
          <label>{{ t('images.model') }}</label>
          <el-select v-model="model" filterable class="fill">
            <el-option v-for="m in models" :key="m.id" :value="m.id" :label="`${m.id} · ${m.provider_name}`" />
          </el-select>
        </div>

        <div class="form-row mb-field">
          <div class="field">
            <label>{{ t('images.size') }}</label>
            <el-select v-model="size" class="fill">
              <el-option v-for="s in sizes" :key="s" :value="s" :label="s" />
            </el-select>
          </div>
          <div class="field">
            <label>{{ t('images.ratio') }}</label>
            <el-select v-model="ratio" class="fill">
              <el-option v-for="r in ratios" :key="r" :value="r" :label="r" />
            </el-select>
          </div>
          <div class="field field-narrow">
            <label>{{ t('images.count') }}</label>
            <!-- 第十三轮：上游接口一次只出 1 张（n>1 会被 400），这里改成"逐张生成"。
                 第十八轮：controls-position="right" —— 两侧加减按钮各占 ~32px，92px 的窄列里
                 内部 input 只剩 8px（数字看不见）；挪到右侧后数字区约 50px。 -->
            <el-input-number v-model="count" :min="1" :max="4" controls-position="right" class="fill" />
          </div>
        </div>

        <div class="field mb-field">
          <label>{{ t('images.promptRow') }}</label>
          <el-input
            v-model="prompt"
            type="textarea"
            :rows="3"
            resize="none"
            :placeholder="t('images.placeholder')"
            :disabled="loading"
          />
        </div>

        <div class="field mb-field">
          <label>{{ t('images.refsLabel') }}</label>
          <!-- 第十三轮：图生图 / 多图合成 —— 支持直接上传（浏览器内压成 data URI），也支持粘 URL -->
          <div class="ref-btns">
            <el-button size="small" :disabled="loading" @click="pickRefs">
              <Icon name="up" :size="14" />{{ t('images.refsUpload') }}
            </el-button>
            <el-button size="small" @click="showRefUrl = !showRefUrl">
              <Icon name="link" :size="14" />{{ t('images.refsUrlBtn') }}
            </el-button>
          </div>
          <div class="hint ref-hint">{{ t('images.refsHint') }}</div>
          <input ref="fileEl" type="file" accept="image/*" multiple class="hide-file" @change="onRefFiles" />
          <div v-if="refs.length" class="ref-thumbs">
            <div v-for="(r, i) in refs" :key="i" class="ref-thumb" :title="`${r.name} · ${formatBytes(dataUriBytes(r.uri))}`">
              <img :src="r.thumb || r.uri" alt="" />
              <span class="rm" @click="refs.splice(i, 1)"><Icon name="x" :size="11" /></span>
              <span class="rn">{{ r.name }}</span>
            </div>
          </div>
          <el-input
            v-if="showRefUrl"
            v-model="refUrls"
            type="textarea"
            :rows="2"
            resize="none"
            :placeholder="t('images.refsPH')"
            :disabled="loading"
            class="ref-urls"
          />
        </div>

        <div class="param-foot">
          <el-alert v-if="err" :title="err" type="error" show-icon :closable="false" class="err-alert" />
          <el-button type="primary" class="go-btn" :loading="loading" :disabled="!prompt.trim()" @click="generate">
            <Icon name="img" :size="15" />{{ t('images.generate') }}
          </el-button>
          <div class="go-tip hint">
            <span v-if="loading && count > 1">{{ t('images.genProgress', { i: progress, n: count }) }}</span>
            <span v-else-if="providerName">{{ t('images.servedBy', { provider: providerName }) }}</span>
            <span v-else>{{ t('images.countTip') }}</span>
          </div>
        </div>
      </div>

      <!-- 右：结果（卡片自己撑满，滚动只发生在缩略图区） -->
      <div class="card result-card">
        <div class="card-h">
          <b>{{ t('images.historyTitle') }}</b>
          <span class="sub">{{ t('images.historySub', { n: images.length }) }}</span>
          <div class="pager">
            <template v-if="images.length">
              <span class="pg" :class="{ off: page <= 1 }" @click="goPage(page - 1)"><Icon name="chev" :size="13" class="flip" /></span>
              <span v-for="p in pageCount" :key="p" class="pg" :class="{ on: p === page }" @click="goPage(p)">{{ p }}</span>
              <span class="pg" :class="{ off: page >= pageCount }" @click="goPage(page + 1)"><Icon name="chev" :size="13" /></span>
            </template>
            <el-button size="small" class="nowrap" :disabled="!images.length" @click="clearAll">{{ t('images.clear') }}</el-button>
          </div>
        </div>

        <!-- 逐张生成中：先占位，出图后自动落到下面网格 -->
        <div v-if="loading" class="thumbs">
          <div v-for="i in count" :key="i" class="th">
            <div class="ph shimmer" />
            <div class="meta"><b>&nbsp;</b><span>&nbsp;</span></div>
          </div>
        </div>

        <div v-else-if="images.length" class="thumbs">
          <div v-for="row in pageImages" :key="row.img.id || row.i" class="th">
            <div class="ph">
              <el-image
                :src="row.img.src"
                fit="cover"
                class="thumb"
                :preview-src-list="images.map((x: any) => x.src)"
                :initial-index="row.i"
                preview-teleported
              >
                <template #error><span class="hint">{{ t('images.imgLoadErr') }}</span></template>
              </el-image>
            </div>
            <!-- 第十七轮: 下载。产物在跨域 CDN 上（不带 CORS 头），直链下载不生效，
                 走 lib/download.ts 里的后端代拉；按钮放在图下方（不做缩略图上的悬浮圆形图标，
                 手机端两列时也不会挤变形）。 -->
            <div class="meta">
              <b :title="row.img.prompt">{{ row.img.prompt || t('images.promptRow') }}</b>
              <span>
                {{ row.img.served }}<template v-if="row.img.ms"> · {{ row.img.ms }}</template>
                <template v-if="row.img.refs"> · {{ t('images.refsUsed', { n: row.img.refs }) }}</template>
              </span>
              <el-button size="small" class="th-btn" :loading="dlId === (row.img.id || row.i)" @click="dlImg(row.img, row.i)">
                <Icon name="dl" :size="14" />{{ t('images.dl') }}
              </el-button>
            </div>
          </div>
        </div>

        <el-empty v-else :description="t('images.empty')" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import Icon from '../components/Icon.vue'
import { api } from '../api'
import { imageSrc } from '../lib/media'
import { downloadMedia, nameFromPrompt, stamp } from '../lib/download'
import { pullHistory, pushHistory, clearHistory, ensureIds } from '../lib/history'
import { pickImage, PRESETS, dataUriBytes, formatBytes, assertBodyBudget } from '../lib/upload'

const router = useRouter()
const { t } = useI18n()
const models = ref<any[]>([])
const model = ref('')
const sizes = ['1K', '2K', '3K', '4K']
const ratios = ['1:1', '3:4', '4:3', '16:9', '9:16', '2:3', '3:2', '21:9']
const size = ref('1K')
const ratio = ref('1:1')
const count = ref(1)              // 逐张生成：每张一次请求（上游 n 必须为 1）
const prompt = ref('')
// 参考图（图生图 / 多图合成）：上传的走 data URI，URL 的直接透传，两类可混用
const refs = ref<any[]>([])
const refUrls = ref('')
const showRefUrl = ref(false)
const fileEl = ref<HTMLInputElement | null>(null)
const MAX_REFS = 5
const loading = ref(false)
const progress = ref(0)
const err = ref('')
const images = ref<any[]>([])
const SKEY = 'gw_image_history'   // 只当本地缓存，真正的历史在服务端
function cacheLocal() {
  try { localStorage.setItem(SKEY, JSON.stringify({ prompt: prompt.value, list: images.value })) } catch { /* 缓存写不进无所谓 */ }
}
async function save() {
  cacheLocal()
  // 写服务端失败必须吭声 —— 以前是 catch 掉静默丢历史，正是「图像还在、视频没了」的根因
  if (!(await pushHistory('images', images.value))) ElMessage.warning(t('history.saveFail'))
}
async function load() {
  let local: any = null
  try { local = JSON.parse(localStorage.getItem(SKEY) || 'null') } catch { local = null }
  const remote = await pullHistory('images')
  if (remote === null) {
    if (local && Array.isArray(local.list)) images.value = ensureIds(local.list, 'img')
    ElMessage.warning(t('history.offline'))
    return
  }
  if (remote.length) {
    images.value = remote
  } else if (local && Array.isArray(local.list) && local.list.length) {
    images.value = ensureIds(local.list, 'img')      // 首次：把浏览器里的旧历史搬上服务端
    await pushHistory('images', images.value)
  } else {
    images.value = []
  }
  if (local && typeof local.prompt === 'string' && local.prompt && !prompt.value) prompt.value = local.prompt
  cacheLocal()
}
async function clearAll() {
  if (!images.value.length) return
  // 老板要求（连同视频页的「清除已完成」）：真删之前必须二次确认 —— 服务端删了不可恢复。
  try {
    await ElMessageBox.confirm(t('images.confirmClear', { n: images.value.length }), t('common.confirm'),
      { type: 'warning', confirmButtonText: t('common.delete'), cancelButtonText: t('common.cancel') })
  } catch { return }
  images.value = []
  cacheLocal()
  if (!(await clearHistory('images'))) ElMessage.warning(t('history.saveFail'))
}

async function loadModels() {
  try {
    const j = await api('/v1/models')
    const all = (j.data || []).filter((x: any) => x.type === 'image')
    models.value = all
    if (all.length && !model.value) model.value = all[0].id
  } catch (e: any) {
    if (e && e.status === 401) { router.push('/login'); return }
    ElMessage.error(t('images.loadErr') + '：' + ((e && e.message) || e))
  }
}

function pickRefs() {
  fileEl.value?.click()
}
async function onRefFiles(ev: Event) {
  const input = ev.target as HTMLInputElement
  for (const f of Array.from(input.files || [])) {
    if (refs.value.length >= MAX_REFS) { ElMessage.warning(t('images.refsMax', { n: MAX_REFS })); break }
    try {
      refs.value.push(await pickImage(f, PRESETS.ref))
    } catch (e: any) {
      ElMessage.error(t('images.refPickErr', { msg: (e && e.message) || e }))
    }
  }
  if (input) input.value = ''
}
/** 上传的 data URI + 手填的 URL，按顺序合成 extra_body.image 的入参 */
function refList(): string[] {
  const urls = refUrls.value.split(/\r?\n/).map((x) => x.trim()).filter(Boolean)
  return [...refs.value.map((r) => r.uri), ...urls]
}

const providerName = ref('')
async function generate() {
  if (!prompt.value.trim() || loading.value) return
  const list = refList()
  try {
    // 素材 + 正文的整包体积要落在网关/Nginx 的请求体上限内（否则 413）
    assertBodyBudget([...list, prompt.value], 1200)
  } catch (e: any) {
    err.value = (e && e.message) || String(e)
    return
  }
  loading.value = true
  err.value = ''
  const total = Math.max(1, Math.min(4, count.value))
  let done = 0
  try {
    for (let i = 1; i <= total; i++) {
      progress.value = i
      const t0 = performance.now()
      // 上游图像接口的 n 必须为 1：多张就在这里逐张调用，每张出来就上屏（进度可见）
      const body: any = { model: model.value, prompt: prompt.value, size: size.value, ratio: ratio.value, n: 1 }
      if (list.length) body.image = list        // 图生图 / 多图合成（网关转成上游的 extra_body.image）
      const j = await api('/v1/images/generations', { method: 'POST', body: JSON.stringify(body) })
      const ms = (performance.now() - t0) | 0
      providerName.value = j.provider_name || j.provider || ''
      const items = (j.data || []).map((d: any, k: number) => ({
        id: `img-${Date.now()}-${i}-${k}`,
        ts: Date.now(),
        src: imageSrc(d),
        served: j.served_model || model.value,
        ms: `${ms}ms`,
        prompt: prompt.value.trim(),
        refs: list.length || undefined,          // 记录本次用了多少张参考图
      })).filter((x: any) => x.src)
      if (!items.length) throw new Error(t('images.emptyResp'))
      done += items.length
      images.value = [...items, ...images.value]
      await save()
    }
  } catch (e: any) {
    // 逐张生成时中途失败：已生成的要留住，并说清是第几张断的
    const detail = (e?.body?.detail) || e?.message || String(e)
    err.value = done
      ? t('images.partialFail', { done, i: Math.min(progress.value, total), msg: detail })
      : detail
  } finally {
    loading.value = false
    progress.value = 0
  }
}

// ── 结果区翻页（纯展示：不裁剪数据，只决定当前渲染哪一页）──
const PAGE_SIZE = 6
const page = ref(1)
const pageCount = computed(() => Math.max(1, Math.ceil(images.value.length / PAGE_SIZE)))
const pageImages = computed(() => images.value
  .slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE)
  .map((img, k) => ({ img, i: (page.value - 1) * PAGE_SIZE + k })))
function goPage(p: number) {
  if (p < 1 || p > pageCount.value) return
  page.value = p
}
// 新图永远插在最前面（逐张生成也是），历史整体换掉时回到第一页
watch(images, () => { page.value = 1 })

onMounted(() => { load(); loadModels() })

// ── 第十七轮: 单张下载 ──
// 文件名用「提示词前缀 + 时间戳」，后端会按 Content-Type 补 .png/.jpg 并以
// Content-Disposition 回传（前端以服务端给的名字落盘，见 lib/download.ts）。
const dlId = ref<any>(null)
async function dlImg(img: any, i: number) {
  if (!img?.src) { ElMessage.error(t('images.dlNoSrc')); return }
  dlId.value = img.id || i
  try {
    await downloadMedia(img.src, `${nameFromPrompt(img.prompt, 'image')}-${stamp()}`)
    ElMessage.success(t('images.dlOk'))
  } catch (e: any) {
    ElMessage.error(t('images.dlFail', { msg: e?.message || String(e) }))
  } finally {
    dlId.value = null
  }
}
</script>

<style scoped>
/* 撑满高度的两栏：左参数卡、右结果卡；滚动只发生在各自内部 */
.param-card { display: flex; flex-direction: column; overflow-y: auto; }
.result-card { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.result-card .thumbs { flex: 1; min-height: 0; overflow-y: auto; align-content: start; }

.mb-field { margin-bottom: 10px; }
/* .field 天生是 flex:1（给 .form-row 横向用的）—— 在竖排卡片里必须收回，
   否则每个字段会纵向撑开，控件之间留下大片空白 */
.param-card > .field { flex: none; }
.fill { width: 100%; }
/* 第十八轮: 92px 太窄（加减按钮吃掉大半），给到 108px 还要保证数字区不被压缩 */
.field-narrow { flex: 0 0 108px; }
.field-narrow :deep(.el-input__inner) { min-width: 30px; }
.ref-btns { display: flex; flex-wrap: wrap; gap: 8px; }
/* 参考图「上传 / URL」两个按钮文案不折行（窄列里被挤成两行是旧版的老毛病） */
.ref-btns :deep(.el-button) { white-space: nowrap; }
.ref-hint { margin-top: 6px; line-height: 1.5; }
.ref-urls { margin-top: 6px; }
.hide-file { display: none; }
.ref-thumbs { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.ref-thumb { position: relative; width: 68px; }
.ref-thumb img { width: 68px; height: 68px; object-fit: cover; border-radius: 6px; border: 1px solid var(--line); display: block; }
.ref-thumb .rn { display: block; font-size: 11px; color: var(--muted-2); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ref-thumb .rm {
  position: absolute; top: -6px; right: -6px; width: 18px; height: 18px;
  display: grid; place-items: center; border-radius: 50%; cursor: pointer;
  background: var(--surface); border: 1px solid var(--line); color: var(--err);
}

.param-foot { margin-top: auto; padding-top: 4px; }
.err-alert { margin-bottom: 8px; }
.go-btn { width: 100%; }
.go-tip { margin-top: 8px; text-align: center; line-height: 1.5; }

.pager { gap: 4px; }
.pg { cursor: pointer; }
.pg.off { opacity: .45; cursor: default; }
.flip { transform: rotate(180deg); }
.pager :deep(.el-button) { margin-left: 6px; }

/* 缩略卡：图在上、标题与下载按钮在下（不做缩略图上的悬浮圆形图标） */
.th .ph { position: relative; }
.thumb { width: 100%; height: 100%; display: block; }
.th .meta b { color: var(--text); }
.th-btn { width: 100%; margin-top: 8px; }
.shimmer { height: 96px; background: linear-gradient(90deg, var(--chip) 25%, var(--surface-2) 37%, var(--chip) 63%); background-size: 400% 100%; animation: ph 1.4s infinite; }
@keyframes ph { 0% { background-position: 100% 0 } 100% { background-position: -100% 0 } }

/* 窄屏：单列后整页滚动，两个卡片都恢复自然高度 */
@media (max-width: 768px) {
  /* 窄屏单列后不再需要「撑满一屏」，改由 .gw-body 整页滚动 */
  .page-fill { height: auto; }
  .img-layout { height: auto; }
  .param-card { overflow: visible; }
  .result-card { flex: none; }
  .result-card .thumbs { overflow: visible; }
  .card-h { flex-wrap: wrap; }
  .th-btn { margin-top: 7px; }
}
</style>
