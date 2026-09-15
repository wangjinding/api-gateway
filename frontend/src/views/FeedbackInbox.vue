<template>
  <div class="page">
    <!-- 第二十二轮：用户反馈（admin 专属）—— 全部账号提交的意见，含图片。 -->
    <div class="filters">
      <el-select v-model="account" :placeholder="t('inbox.account')" clearable size="small" class="sel-acct">
        <el-option :label="t('inbox.all')" value="" />
        <el-option v-for="a in accounts" :key="a" :value="a" :label="a" />
      </el-select>
      <el-select v-model="status" :placeholder="t('inbox.status')" clearable size="small" class="sel-st">
        <el-option :label="t('inbox.allStatus')" value="" />
        <el-option v-for="s in FEEDBACK_STATUSES" :key="s" :value="s" :label="t(`fbStatus.${s}`)" />
      </el-select>
      <el-select v-model="arch" size="small" class="sel-arch">
        <el-option :label="t('inbox.archActive')" value="active" />
        <el-option :label="t('inbox.archOnly')" value="arch" />
        <el-option :label="t('inbox.archAll')" value="" />
      </el-select>
      <el-input v-model="kw" size="small" :placeholder="t('inbox.kw')" clearable class="kw-in">
        <template #append><el-button @click="apply">{{ t('common.search') }}</el-button></template>
      </el-input>
      <span class="spacer" />
      <el-button v-if="filtered.length !== items.length" size="small" text @click="clear">{{ t('logs.clearFilter') }}</el-button>
      <el-button size="small" :disabled="loading" @click="load()">
        <Icon name="refresh" :size="13" />
        <span>{{ t('common.refresh') }}</span>
      </el-button>
    </div>

    <div class="card">
      <div class="card-h">
        <b>{{ t('nav.menufeedbackinbox') }}</b>
        <span class="sub">{{ t('inbox.count', { n: filtered.length }) }}</span>
        <div class="pager">
          <span v-if="filtered.length > PAGE_SIZE" class="pg" :class="{ dis: page <= 1 }" @click="go(page - 1)">
            <Icon name="chev" :size="12" class="flip" />
          </span>
          <span v-if="filtered.length > PAGE_SIZE" class="hint">{{ page }} / {{ pages }}</span>
          <span v-if="filtered.length > PAGE_SIZE" class="pg" :class="{ dis: page >= pages }" @click="go(page + 1)">
            <Icon name="chev" :size="12" />
          </span>
        </div>
      </div>

      <div v-loading="loading" class="fb-list">
        <div v-if="!filtered.length" class="hint">
          <span>{{ items.length ? t('inbox.emptyFiltered', { n: items.length }) : t('inbox.empty') }}</span>
          <el-button v-if="items.length" size="small" text @click="clear">{{ t('logs.clearFilter') }}</el-button>
        </div>

        <div v-for="it in paged" :key="it.id" class="fb-item">
          <div class="fb-meta">
            <span class="ts">{{ fmtTs(it.created) }}</span>
            <span class="badge m">{{ it.account }}</span>
            <span class="badge" :class="FEEDBACK_STATUS_CLASS[it.status] || 'm'">{{ t(`fbStatus.${it.status}`) }}</span>
            <span v-if="it.images?.length" class="pill mute">
              <Icon name="img" :size="11" />
              {{ t('inbox.imgCount', { n: it.images.length }) }}
            </span>
            <span v-if="it.archived" class="badge m">{{ t('inbox.archived') }}</span>
            <span class="spacer" />
            <el-button size="small" @click="openHandle(it)">{{ t('inbox.handle') }}</el-button>
            <el-button size="small" text :loading="busy === it.id" @click="toggleArchive(it)">
              {{ it.archived ? t('inbox.unarchive') : t('inbox.archive') }}
            </el-button>
            <el-button size="small" text type="danger" @click="remove(it)">{{ t('inbox.delete') }}</el-button>
          </div>
          <div class="fb-text">{{ it.content }}</div>
          <FeedbackImages :item="it" />
          <!-- 处理记录：谁在什么时候把它推到哪一档、留了什么说明，逐条可查 -->
          <FeedbackTimeline :item="it" />
        </div>
      </div>
    </div>

    <!-- 处理：改进度 + 给提交人留一句话（提交人在「我提交过的」里看到） -->
    <el-dialog v-model="handleShow" :title="t('inbox.dialogTitle')" width="min(520px, 92vw)">
      <div class="field">
        <label>{{ t('inbox.status') }}</label>
        <el-radio-group v-model="form.status">
          <el-radio-button v-for="s in FEEDBACK_STATUSES" :key="s" :value="s">
            {{ t(`fbStatus.${s}`) }}
          </el-radio-button>
        </el-radio-group>
      </div>
      <div class="field mt">
        <label>{{ t('inbox.noteLabel') }}</label>
        <el-input
          v-model="form.note"
          type="textarea"
          :rows="3"
          :maxlength="500"
          show-word-limit
          resize="none"
          :placeholder="t('inbox.notePlaceholder')"
        />
      </div>
      <template #footer>
        <el-button @click="handleShow = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ t('inbox.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import Icon from '../components/Icon.vue'
import FeedbackImages from '../components/FeedbackImages.vue'
import FeedbackTimeline from '../components/FeedbackTimeline.vue'
import { allFeedback, updateFeedback, archiveFeedback, adminDeleteFeedback, FEEDBACK_STATUSES, FEEDBACK_STATUS_CLASS, type FeedbackItem } from '../lib/feedback'

const { t } = useI18n()
const PAGE_SIZE = 10

const items = ref<FeedbackItem[]>([])
const loading = ref(false)
const account = ref('')
const status = ref('')
const kw = ref('')
const applied = ref('')
const page = ref(1)
const arch = ref<'active' | 'arch' | ''>('active')
const busy = ref('')

// 处理弹窗
const handleShow = ref(false)
const saving = ref(false)
const form = ref<{ id: string; status: FeedbackItem['status']; note: string }>({ id: '', status: 'pending', note: '' })

function openHandle(it: FeedbackItem) {
  form.value = { id: it.id, status: it.status, note: it.note || '' }
  handleShow.value = true
}

async function save() {
  saving.value = true
  try {
    const updated = await updateFeedback(form.value.id, form.value.status, form.value.note)
    // 就地替换：不整表重拉，筛选/页码/滚动位置都不动
    const i = items.value.findIndex((x) => x.id === updated.id)
    if (i >= 0) items.value[i] = updated
    handleShow.value = false
    ElMessage.success(t('inbox.saved'))
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  } finally {
    saving.value = false
  }
}

async function toggleArchive(it: FeedbackItem) {
  busy.value = it.id
  try {
    const updated = await archiveFeedback(it.id, !it.archived)
    const i = items.value.findIndex((x) => x.id === updated.id)
    if (i >= 0) items.value[i] = updated
    ElMessage.success(updated.archived ? t('inbox.archOk') : t('inbox.unarchOk'))
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  } finally {
    busy.value = ''
  }
}
async function remove(it: FeedbackItem) {
  try {
    await ElMessageBox.confirm(t('inbox.deleteConfirm'), t('inbox.delete'), {
      type: 'warning',
      confirmButtonText: t('inbox.delete'),
      cancelButtonText: t('common.cancel'),
    })
  } catch {
    return
  }
  try {
    await adminDeleteFeedback(it.id)
    items.value = items.value.filter((x) => x.id !== it.id)
    ElMessage.success(t('inbox.deleted'))
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  }
}

/** 账号下拉直接由数据里出现过的账号生成 —— 不给服务端再加一个"谁提过反馈"的接口。 */
const accounts = computed(() => {
  const set = new Set<string>()
  items.value.forEach((it) => it.account && set.add(it.account))
  return Array.from(set).sort()
})

const filtered = computed(() => {
  const k = applied.value.trim().toLowerCase()
  return items.value.filter((it) => {
    if (account.value && it.account !== account.value) return false
    if (status.value && it.status !== status.value) return false
    if (arch.value === 'active' && it.archived) return false
    if (arch.value === 'arch' && !it.archived) return false
    if (!k) return true
    return it.content.toLowerCase().includes(k) || it.account.toLowerCase().includes(k)
  })
})

const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / PAGE_SIZE)))
const paged = computed(() => filtered.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE))

// 任何筛选变化都回第一页，免得停在一个已经空的页码上
watch([filtered, account, status], () => { if (page.value > pages.value) page.value = 1 })

function apply() { applied.value = kw.value; page.value = 1 }
function clear() { kw.value = ''; applied.value = ''; account.value = ''; status.value = ''; arch.value = 'active'; page.value = 1 }
function go(p: number) { page.value = Math.min(Math.max(1, p), pages.value) }

function fmtTs(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function load() {
  loading.value = true
  try {
    items.value = await allFeedback()
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.sel-acct { width: 150px; }
.sel-st { width: 128px; }
.sel-arch { width: 128px; }
.kw-in { width: 210px; }
.filters .spacer { flex: 1; }
.fb-list { min-height: 60px; }
.fb-item {
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 12px 14px;
  margin-bottom: 12px;
}
.fb-item:last-child { margin-bottom: 0; }
.fb-meta { display: flex; align-items: center; gap: 8px; }
.fb-meta .ts { font-size: 11.5px; color: var(--muted-2); font-variant-numeric: tabular-nums; }
.fb-meta .pill { display: inline-flex; align-items: center; gap: 4px; }
.fb-text { margin-top: 6px; font-size: 13px; line-height: 1.65; white-space: pre-wrap; word-break: break-word; }
.field { margin-bottom: 12px; }
.field.mt { margin-top: 4px; }
.field label { display: block; margin-bottom: 6px; font-size: 12.5px; color: var(--muted); }
.pg.dis { opacity: .35; cursor: default; }
.flip { transform: rotate(180deg); }
@media (max-width: 768px) {
  .sel-acct { width: 132px; }
  .sel-st { width: 112px; }
  .kw-in { width: 100%; }
}
</style>
