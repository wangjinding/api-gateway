<template>
  <div class="page">
    <!-- 第二十二轮：意见反馈（普通账号 / 管理员都能提）。
         左「写」右「我提交过的」在桌面并排，窄屏自动上下堆叠。 -->
    <div class="fb-wrap">
      <div class="card">
        <div class="card-h">
          <b>{{ t('nav.menufeedback') }}</b>
          <span class="sub">{{ t('feedback.imagesHint', { n: FEEDBACK_MAX_IMAGES }) }}</span>
        </div>

        <div class="field">
          <label>{{ t('feedback.write') }}</label>
          <el-input
            v-model="content"
            type="textarea"
            :rows="7"
            :maxlength="2000"
            show-word-limit
            resize="none"
            :placeholder="t('feedback.placeholder')"
          />
        </div>

        <div class="fb-picks">
          <div v-for="(m, i) in picks" :key="i" class="fb-pick">
            <img :src="m.thumb || m.uri" :alt="m.name" />
            <button class="fb-del" type="button" :title="t('feedback.remove')" @click="remove(i)">
              <Icon name="x" :size="12" />
            </button>
          </div>
          <button
            v-if="picks.length < FEEDBACK_MAX_IMAGES"
            class="fb-add"
            type="button"
            :title="t('feedback.attach')"
            @click="fileEl?.click()"
          >
            <Icon name="plus" :size="18" />
            <span>{{ t('feedback.attach') }}</span>
          </button>
          <input ref="fileEl" class="fb-file" type="file" accept="image/*" multiple @change="onPick" />
        </div>

        <div class="fb-actions">
          <el-button type="primary" :loading="sending" :disabled="!content.trim()" @click="submit">
            {{ t('feedback.submit') }}
          </el-button>
          <span v-if="picks.length" class="hint">{{ t('inbox.imgCount', { n: picks.length }) }}</span>
        </div>
      </div>

      <div class="card">
        <div class="card-h">
          <b>{{ t('feedback.mine') }}</b>
          <span class="sub">{{ t('feedback.mineSub') }}</span>
          <div class="pager">
            <el-button size="small" text :disabled="loading" @click="load()">
              <Icon name="refresh" :size="13" />
              <span>{{ t('common.refresh') }}</span>
            </el-button>
          </div>
        </div>

        <div class="fb-filter">
          <el-select v-model="mineStatus" size="small" class="sel-mine">
            <el-option :label="t('feedback.statusAll')" value="" />
            <el-option v-for="st in FEEDBACK_STATUSES" :key="st" :value="st" :label="t(`fbStatus.${st}`)" />
          </el-select>
        </div>

        <div v-if="!mineView.length" class="hint">{{ t('feedback.empty') }}</div>
        <div v-else class="fb-list">
          <div v-for="it in mineView" :key="it.id" class="fb-item">
            <div class="fb-meta">
              <span class="ts">{{ fmtTs(it.created) }}</span>
              <!-- 处理进度：提交人看得到，管理员在「用户反馈」里推进 -->
              <span class="badge" :class="FEEDBACK_STATUS_CLASS[it.status] || 'm'">
                {{ t(`fbStatus.${it.status}`) }}
              </span>
              <span v-if="it.images?.length" class="pill mute">{{ t('inbox.imgCount', { n: it.images.length }) }}</span>
              <span v-if="it.status === 'pending'" class="spacer" />
              <!-- 只有还停在「待处理」的单子能撤：管理员一动就不给了（服务端也把着关） -->
              <el-button
                v-if="it.status === 'pending'"
                size="small" text type="danger"
                :loading="revoking === it.id"
                @click="revoke(it)"
              >{{ t('feedback.revoke') }}</el-button>
            </div>
            <div class="fb-text">{{ it.content }}</div>
            <FeedbackImages :item="it" />
            <!-- 处理记录：提交 + 管理员的每一次动作（含说明与是谁处理的） -->
            <FeedbackTimeline :item="it" />
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import Icon from '../components/Icon.vue'
import FeedbackImages from '../components/FeedbackImages.vue'
import FeedbackTimeline from '../components/FeedbackTimeline.vue'
import {
  FEEDBACK_MAX_IMAGES, FEEDBACK_STATUSES, FEEDBACK_STATUS_CLASS, myFeedback, pickFeedbackImage,
  submitFeedback, withdrawFeedback, type FeedbackItem,
} from '../lib/feedback'
import type { PickedMedia } from '../lib/upload'

const { t } = useI18n()

const content = ref('')
const picks = ref<PickedMedia[]>([])
const mine = ref<FeedbackItem[]>([])
const sending = ref(false)
const loading = ref(false)
const mineStatus = ref('')    // 「我提交过的」状态过滤：''=全部
const mineView = computed(() =>
  mineStatus.value ? mine.value.filter((it) => it.status === mineStatus.value) : mine.value,
)
const revoking = ref('')      // 正在撤回的那条 id（按钮 loading 用）
const fileEl = ref<HTMLInputElement | null>(null)

function fmtTs(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function load() {
  loading.value = true
  try {
    mine.value = await myFeedback()
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  } finally {
    loading.value = false
  }
}

async function onPick(ev: Event) {
  const el = ev.target as HTMLInputElement
  const files = Array.from(el.files || [])
  el.value = ''            // 先清空：同一个文件再选一次也要能触发 change
  for (const f of files) {
    if (picks.value.length >= FEEDBACK_MAX_IMAGES) {
      ElMessage.warning(t('feedback.imagesHint', { n: FEEDBACK_MAX_IMAGES }))
      break
    }
    try {
      // pickImage 内部已生成缩略图（256px，列表 84px 显示够用），随包一起上送：
      // 服务端 JSON 只存它，列表页因此不用再逐张取原图。
      picks.value.push(await pickFeedbackImage(f))
    } catch (e: any) {
      ElMessage.error(e?.message || String(e))
    }
  }
}

function remove(i: number) {
  picks.value.splice(i, 1)
}

async function submit() {
  if (!content.value.trim()) {
    ElMessage.warning(t('feedback.needContent'))
    return
  }
  sending.value = true
  try {
    await submitFeedback({ content: content.value, images: picks.value })
    content.value = ''
    picks.value = []
    ElMessage.success(t('feedback.submitted'))
    await load()
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
  } finally {
    sending.value = false
  }
}

// 撤回＝真删，所以必须二次确认（与图像「清空历史」同一条规矩）。
// 前端只在 pending 时给按钮，但服务端才是权威：管理员可能刚好在你点开确认框时动手，
// 那种情况下 DELETE 会回 400，这里把服务端的话直接展示出来，别假装成功。
async function revoke(it: FeedbackItem) {
  try {
    await ElMessageBox.confirm(t('feedback.revokeBody', { n: it.content.slice(0, 40) }), t('feedback.revokeTitle'), {
      type: 'warning',
      confirmButtonText: t('feedback.revoke'),
      cancelButtonText: t('common.cancel'),
    })
  } catch { return }
  revoking.value = it.id
  try {
    await withdrawFeedback(it.id)
    mine.value = mine.value.filter((x) => x.id !== it.id)
    ElMessage.success(t('feedback.revoked'))
  } catch (e: any) {
    ElMessage.error(e?.message || String(e))
    await load()          // 状态可能已被管理员改掉，拉一次以真实状态为准
  } finally {
    revoking.value = ''
  }
}

onMounted(load)
</script>

<style scoped>
.fb-wrap { display: grid; grid-template-columns: 1.15fr 1fr; gap: var(--gap); align-items: start; }
@media (max-width: 900px) { .fb-wrap { grid-template-columns: 1fr; } }
.fb-picks { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; }
.fb-pick { position: relative; width: 84px; height: 84px; border-radius: 10px; overflow: hidden; border: 1px solid var(--line); }
.fb-pick img { width: 100%; height: 100%; object-fit: cover; display: block; }
.fb-del {
  position: absolute; top: 3px; right: 3px; width: 20px; height: 20px; padding: 0;
  display: grid; place-items: center; border: 0; border-radius: 7px; cursor: pointer;
  background: rgba(0, 0, 0, .55); color: #fff;
}
.fb-add {
  width: 84px; height: 84px; border: 1px dashed var(--line); border-radius: 10px;
  background: var(--surface-2); color: var(--muted); cursor: pointer;
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px;
  font-size: 11px; padding: 0 4px; text-align: center;
}
.fb-add:hover { border-color: var(--primary); color: var(--primary); }
.fb-file { display: none; }
.fb-actions { display: flex; align-items: center; gap: 10px; margin-top: 14px; }
.fb-list { display: flex; flex-direction: column; }
.fb-item {
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 12px 14px;
  margin-bottom: 12px;
}
.fb-item:last-child { margin-bottom: 0; }
.fb-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.fb-meta .spacer { flex: 1; }
.fb-meta .ts { font-size: 11.5px; color: var(--muted-2); font-variant-numeric: tabular-nums; }
.fb-filter { display: flex; margin: 2px 0 6px; }
.sel-mine { width: 128px; }
.fb-text { margin-top: 6px; font-size: 12.5px; line-height: 1.62; white-space: pre-wrap; word-break: break-word; }
</style>
