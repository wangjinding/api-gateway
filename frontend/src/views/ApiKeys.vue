<template>
  <div class="page">
    <div class="card">
      <div class="card-h">
        <b>{{ t('settings.myKeys') }}</b>
        <span class="sub">{{ keys.length }} / {{ limit === null ? '∞' : limit }}</span>
        <div class="pager">
          <!-- 标签输入留在页头：创建时仍可给 Key 起名（行为与旧版一致） -->
          <el-input
            v-model="label"
            size="small"
            class="label-inp"
            :placeholder="t('settings.keyLabel')"
            clearable
            @keyup.enter="create"
          />
          <el-tooltip :disabled="!atLimit" :content="t('settings.keyLimitFull')">
            <span>
              <el-button type="primary" size="small" :loading="creating" :disabled="atLimit" @click="create">
                <Icon name="plus" :size="14" />
                <span>{{ atLimit ? t('settings.createKey') + '（' + t('settings.keyLimitFull') + '）' : t('settings.createKey') }}</span>
              </el-button>
            </span>
          </el-tooltip>
        </div>
      </div>

      <!-- 第八轮补: 后端在 GET /auth/apikeys 里回 key_limit，前端据此提前禁用按钮 ——
           到顶了就别再发那个必然 400 的请求，提示也走人话而不是错误码。 -->
      <div v-if="atLimit" class="warn-bar">
        <Icon name="bell" :size="15" />
        <span>{{ t('settings.keyLimitFull') }}</span>
      </div>

      <div v-if="keys.length">
        <div v-for="k in keys" :key="k.id" class="key-row">
          <div class="flex-1">
            <div class="key-t">
              <b>{{ k.label || t('common.none') }}</b>
            </div>
            <div class="code key-val">
              <span v-if="k.key">{{ k.key }}</span>
              <span v-else class="muted-2">{{ k.hint }}…… {{ t('settings.keyLegacyNoEcho') }}</span>
            </div>
            <div class="hint key-meta">{{ t('settings.keyCreated') }} · {{ fmtTs(k.created) }}</div>
          </div>
          <div class="key-acts">
            <el-button size="small" :disabled="!k.key" @click="copyKey(k.key)">
              <Icon name="copy" :size="13" />
              <span>{{ t('common.copy') }}</span>
            </el-button>
            <el-button size="small" type="danger" plain @click="remove(k.id)">
              <Icon name="trash" :size="13" />
              <span>{{ t('common.delete') }}</span>
            </el-button>
          </div>
        </div>
      </div>
      <el-empty v-else :description="t('settings.noKey')" :image-size="80" />

      <div class="sep" />

      <!-- 调用方式：Key 页面直接给一份调用说明，别让人去翻 README -->
      <div class="card-h">
        <b>{{ t('apidoc.title') }}</b>
        <span class="sub">{{ base }}</span>
      </div>

      <div class="log-detail">
        <div>
          <b>{{ t('apidoc.baseUrl') }}</b>
          <span class="code">{{ base }}</span>
          <el-button text type="primary" size="small" @click="copy(base)">
            <Icon name="copy" :size="12" />
            <span>{{ t('apidoc.copyBase') }}</span>
          </el-button>
        </div>
        <div><b>{{ t('apidoc.auth') }}</b> <span class="code">{{ t('apidoc.authVal') }}</span></div>
        <div><b>{{ t('apidoc.modelPick') }}</b> <span>{{ t('apidoc.modelPickVal') }}</span></div>
        <div><b>{{ t('apidoc.ctype') }}</b> <span>{{ t('apidoc.ctypeVal') }}</span></div>
      </div>

      <div class="call-acts">
        <el-button size="small" @click="copy(examples[0].code)">
          <Icon name="copy" :size="13" />
          <span>{{ t('apidoc.copyCmd') }} curl</span>
        </el-button>
        <el-button size="small" @click="scrollDoc">
          <Icon name="chevD" :size="13" />
          <span>{{ t('apidoc.title') }}</span>
        </el-button>
        <span class="hint call-note">{{ t('apidoc.intro') }}</span>
      </div>

      <div ref="docRef" class="doc">
        <div class="sub-title">{{ t('apidoc.endpoints') }}</div>
        <el-table :data="endpoints" size="small">
          <el-table-column prop="method" :label="t('apidoc.colMethod')" width="80">
            <template #default="{ row }">
              <span class="badge m">{{ row.method }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('apidoc.colPath')" min-width="230">
            <template #default="{ row }"><span class="code">{{ row.path }}</span></template>
          </el-table-column>
          <el-table-column prop="desc" :label="t('apidoc.colDesc')" min-width="280" />
        </el-table>

        <div v-for="ex in examples" :key="ex.title" class="ex">
          <div class="sub-title">
            {{ ex.title }}
            <el-button text type="primary" size="small" @click="copy(ex.code)">
              <Icon name="copy" :size="12" />
              <span>{{ t('apidoc.copyCmd') }}</span>
            </el-button>
          </div>
          <pre class="code pre">{{ ex.code }}</pre>
        </div>

        <div class="sub-title">{{ t('apidoc.notes') }}</div>
        <ul class="notes">
          <li v-for="(n, i) in notes" :key="i">{{ n }}</li>
        </ul>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import Icon from '../components/Icon.vue'

const { t } = useI18n()
const keys = ref<any[]>([])
const label = ref('')
const creating = ref(false)
const limit = ref<number | null>(null)   // null = 不限（admin）
const base = window.location.origin

// 端点表：与 app/main.py 的真实路由一一对应（别写成 README 里才有的东西）
const endpoints = computed(() => [
  { method: 'GET', path: '/v1/models', desc: t('apidoc.epModels') },
  { method: 'POST', path: '/v1/chat/completions', desc: t('apidoc.epChat') },
  { method: 'POST', path: '/v1/images/generations', desc: t('apidoc.epImages') },
  { method: 'POST', path: '/v1/videos', desc: t('apidoc.epVideoCreate') },
  { method: 'GET', path: '/v1/videos/{video_id}?model=...', desc: t('apidoc.epVideoPoll') },
  { method: 'GET', path: '/history/{kind}', desc: t('apidoc.epHistory') },
  { method: 'GET', path: '/logs', desc: t('apidoc.epLogs') },
  { method: 'GET', path: '/metrics', desc: t('apidoc.epMetrics') },
  { method: 'GET', path: '/health', desc: t('apidoc.epHealth') },
])

const notes = computed(() => [
  t('apidoc.noteVision'), t('apidoc.noteN1'), t('apidoc.noteVideo'),
  t('apidoc.notePoll'), t('apidoc.noteMedia'), t('apidoc.noteKey'), t('apidoc.noteAudit'),
])

// 示例里的 Key 用占位符 $GW_KEY（不把真 Key 渲染进文档；本页的 Key 明文只在上方列表里回显）
const examples = computed(() => {
  const H = `-H "Authorization: Bearer $GW_KEY" -H "Content-Type: application/json"`
  return [
    // 示例里的注释与示例文案也走 i18n：英文界面下照抄示例不会看到中文
    { title: t('apidoc.exChat'), code:
`curl ${base}/v1/chat/completions \\
  ${H} \\
  -d '{"model":"wjd","messages":[{"role":"user","content":"${t('apidoc.sampleChat')}"}]}'` },
    { title: t('apidoc.exImg'), code:
`curl ${base}/v1/images/generations \\
  ${H} \\
  -d '{"model":"agnes-image-2.5-flash","prompt":"${t('apidoc.sampleImg')}","size":"1K","ratio":"16:9","n":1}'` },
    { title: t('apidoc.exVideo'), code:
`# 1) ${t('apidoc.sampleStep1')}
curl ${base}/v1/videos \\
  ${H} \\
  -d '{"model":"agnes-video-2.5-flash","prompt":"${t('apidoc.sampleVideo')}","mode":"text","seconds":"5","size":"720P","aspect_ratio":"16:9"}'
# → {"video_id":"task_xxx","status":"pending"}

# 2) ${t('apidoc.sampleStep2')}
curl "${base}/v1/videos/task_xxx?model=agnes-video-2.5-flash" \\
  -H "Authorization: Bearer $GW_KEY"` },
    { title: t('apidoc.exPy'), code:
`from openai import OpenAI

c = OpenAI(base_url="${base}/v1", api_key="wjd-xxxxxxxx")
r = c.chat.completions.create(model="wjd", messages=[{"role": "user", "content": "${t('apidoc.sampleChat')}"}])
print(r.choices[0].message.content)` },
  ]
})

// 「查看接口文档」：滚动到同卡片下方的端点 / 示例 / 注意事项（文档始终可见，不做折叠）
const docRef = ref<HTMLElement | null>(null)
function scrollDoc() {
  docRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success(t('common.copyOk'))
  } catch { ElMessage.warning(t('common.copyFail')) }
}

const atLimit = computed(() => limit.value !== null && keys.value.length >= limit.value)

function fmtTs(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function load() {
  try {
    const j = await api('/auth/apikeys')
    keys.value = j.keys || []
    limit.value = j.key_limit ?? null
  } catch (e: any) { ElMessage.error(e?.body?.detail || e?.message || String(e)) }
}

async function create() {
  if (atLimit.value) { ElMessage.warning(t('settings.keyLimitFull')); return }
  creating.value = true
  try {
    await api('/auth/apikeys', { method: 'POST', body: JSON.stringify({ label: label.value.trim() }) })
    label.value = ''
    await load()
  } catch (e: any) {
    ElMessage.error(e?.body?.detail || String(e?.message || e))
  } finally {
    creating.value = false
  }
}

async function remove(id: string) {
  try {
    await ElMessageBox.confirm(t('settings.confirmDeleteKey'), t('settings.confirm'), { type: 'warning' })
  } catch { return }
  try {
    await api(`/auth/apikeys/${id}`, { method: 'DELETE' })
    await load()
  } catch (e: any) { ElMessage.error(e?.body?.detail || String(e?.message || e)) }
}

async function copyKey(k: string) {
  try {
    await navigator.clipboard.writeText(k)
    ElMessage.success(t('common.copyOk'))
  } catch { ElMessage.warning(t('common.copyFail')) }
}

onMounted(load)
</script>

<style scoped>
.label-inp { width: 190px; }
.warn-bar {
  display: flex; align-items: center; gap: 8px;
  background: var(--warn-soft); color: var(--warn);
  border-radius: 10px; padding: 9px 12px; font-size: 12px; margin-bottom: 12px;
}
.key-t { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.key-val { margin-top: 5px; color: var(--muted); word-break: break-all; }
.key-meta { margin-top: 4px; }
.key-acts { display: flex; gap: 7px; flex: none; }
.key-acts .gw-icon, .call-acts .gw-icon { margin-right: 5px; }
.log-detail > div { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.log-detail > div + div { margin-top: 4px; }
.call-acts { display: flex; align-items: center; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.call-note { margin-left: auto; max-width: 54%; text-align: right; line-height: 1.6; }
.sub-title { display: flex; align-items: center; gap: 4px; font-weight: 600; font-size: 13px; margin: 14px 0 8px; }
.pre {
  background: var(--surface-2); border: 1px solid var(--line); color: var(--text);
  padding: 10px 12px; border-radius: 10px; margin: 0; font-size: 12px; line-height: 1.6;
  overflow-x: auto; white-space: pre;
}
.ex { margin-bottom: 4px; }
.notes { margin: 0; padding-left: 18px; color: var(--muted); font-size: 12.5px; line-height: 1.9; }

@media (max-width: 768px) {
  .label-inp { width: 100%; }
  .key-row { flex-wrap: wrap; }
  .call-note { margin-left: 0; max-width: 100%; text-align: left; }
}
</style>
