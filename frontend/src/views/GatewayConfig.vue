<template>
  <div class="page">
    <el-alert v-if="err" class="mb" type="error" show-icon :closable="false"
              :title="t('gc.errTitle')" :description="err" />

    <!-- 文件信息 -->
    <div class="card">
      <div class="card-h">
        <b>{{ t('gc.file') }}</b>
        <span class="sub">{{ t('gc.fileSub') }}</span>
        <span class="flex-1" />
        <el-button size="small" :loading="loading" @click="load">{{ t('common.refresh') }}</el-button>
      </div>
      <div class="set-row"><div class="lb">{{ t('gc.path') }}</div><div class="rt code">{{ meta.path || '—' }}</div></div>
      <div class="set-row">
        <div class="lb">{{ t('gc.restartKeys') }}</div>
        <div class="rt wrap">
          <span v-for="k in meta.restart_keys || []" :key="k" class="pill warn code">{{ k }}</span>
        </div>
      </div>
    </div>

    <!-- YAML 全文编辑器 -->
    <div class="card mt">
      <div class="card-h">
        <b>{{ t('gc.editorTitle') }}</b>
        <span class="sub">{{ t('gc.editorSub') }}</span>
        <span class="flex-1" />
        <el-button size="small" @click="backupCfg">{{ t('gc.backup') }}</el-button>
        <el-button size="small" plain @click="pickRestore">{{ t('gc.restore') }}</el-button>
        <el-button type="primary" size="small" :loading="saving" @click="saveYaml">{{ t('gc.save') }}</el-button>
        <span v-if="restartPending.length" class="pill warn restart-pill">{{ t('gc.restartPill') }}</span>
      </div>
      <input ref="restoreInput" type="file" accept=".json" class="hidden" @change="onRestoreFile" />

      <textarea v-model="yamlText" class="editor" spellcheck="false"></textarea>

      <div class="hint">{{ t('gc.saveTip') }}</div>
      <el-alert v-if="savedOk" class="mt" type="success" :closable="false" :title="t('gc.saveOk')" />
      <el-alert v-if="restartPending.length" class="mt" type="info" show-icon :closable="false"
                :title="t('gc.restartSaved', { keys: restartPending.join('、') })" />
      <el-alert v-for="(w, i) in warns" :key="i" class="mt" type="warning" :closable="false" :title="w" />
    </div>

    <!-- API Key 环境变量 -->
    <div class="card mt">
      <div class="card-h">
        <b>{{ t('gc.envTitle') }}</b>
        <span class="sub">{{ t('gc.envSub') }}</span>
        <span class="flex-1" />
      </div>
      <div v-if="!envKeys.length" class="hint">{{ t('gc.envEmpty') }}</div>
      <div v-for="e in envKeys" :key="e.env" class="set-row">
        <div class="lb flex-1">
          <span class="code">{{ e.env }}</span>
          <span class="pill" :class="e.set ? 'ok' : 'err'">{{ e.set ? t('gc.envSet') : t('gc.envMissing') }}</span>
          <div class="sub">{{ e.provider_name || e.provider }}</div>
        </div>
        <div class="rt env-rt">
          <el-input v-model="e.value" size="small" :type="e.show ? 'text' : 'password'" class="env-in" />
          <el-button size="small" text @click="e.show = !e.show">{{ t(e.show ? 'gc.envHide' : 'gc.envShow') }}</el-button>
          <el-button size="small" text :disabled="!e.value" @click="copyEnv(e)">{{ t('gc.envCopy') }}</el-button>
          <el-button size="small" type="primary" plain :loading="savingEnv" @click="saveEnv(e)">{{ t('gc.save') }}</el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'

const { t } = useI18n()

const loading = ref(false)
const saving = ref(false)
const savingEnv = ref(false)
const err = ref('')
const warns = ref<string[]>([])
const savedOk = ref(false)
const restartPending = ref<string[]>([])
const meta = ref<any>({})
const yamlText = ref('')
const restoreInput = ref<HTMLInputElement | null>(null)
interface EnvRow { env: string; provider: string; provider_name: string; set: boolean; value: string; show: boolean }
const envKeys = ref<EnvRow[]>([])

function copyText(text: string): boolean {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text)
      return true
    }
  } catch { /* fall through */ }
  const ta = document.createElement('textarea')
  ta.value = text
  document.body.appendChild(ta)
  ta.select()
  try { document.execCommand('copy') } catch { /* ignore */ }
  document.body.removeChild(ta)
  return true
}

async function load() {
  loading.value = true
  err.value = ''
  savedOk.value = false
  restartPending.value = []
  try {
    const body = await api('/config')
    meta.value = body.meta || {}
    yamlText.value = body.yaml || ''
    const envs = (body.env || {}) as Record<string, any>
    envKeys.value = Object.keys(envs).map((env) => ({
      env,
      provider: envs[env].provider || '—',
      provider_name: envs[env].provider_name || '',
      set: !!envs[env].set,
      value: envs[env].value || '',
      show: false,
    }))
    // 第二十七轮: 路由告警（meta.route_warnings）随路由功能删除，体检结论只看 warnings
    warns.value = body.meta?.warnings || []
  } catch (e: any) {
    err.value = (e?.body?.detail || e?.message || String(e))
  } finally {
    loading.value = false
  }
}

async function saveYaml() {
  err.value = ''
  savedOk.value = false
  saving.value = true
  try {
    const r = await api('/config', { method: 'PUT', body: JSON.stringify({ yaml: yamlText.value }) })
    restartPending.value = r.restart_required || []
    warns.value = r.warnings || []
    savedOk.value = true
    ElMessage.success(t('gc.saveOk'))
  } catch (e: any) {
    const d = e?.body?.detail
    err.value = typeof d === 'string' ? d : (d ? JSON.stringify(d) : (e?.message || String(e)))
  } finally {
    saving.value = false
  }
}

function backupCfg() {
  const envs: Record<string, string> = {}
  for (const e of envKeys.value) envs[e.env] = e.value
  const data = { kind: 'api-gateway-backup', version: 1, config: yamlText.value, envs }
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = 'gateway-backup.json'
  a.click()
  URL.revokeObjectURL(a.href)
  ElMessage.success(t('gc.backupOk'))
}
function pickRestore() { restoreInput.value?.click() }
function onRestoreFile(ev: Event) {
  const f = (ev.target as HTMLInputElement).files?.[0]
  if (!f) return
  const r = new FileReader()
  r.onload = () => {
    try {
      const parsed = JSON.parse(String(r.result || '{}'))
      const cfg = typeof parsed?.config === 'string' ? parsed.config : ''
      const rawEnvs = parsed && parsed.envs && typeof parsed.envs === 'object' ? parsed.envs : {}
      const envs: Record<string, string> = {}
      for (const [k, v] of Object.entries(rawEnvs as any)) envs[k] = String(v)
      if (!cfg.trim()) throw new Error('no config')
      ElMessageBox.confirm(
        t('gc.restoreConfirm', { envs: Object.keys(envs).length }),
        t('gc.restore'),
        { type: 'warning', confirmButtonText: t('gc.restoreGo'), cancelButtonText: t('gc.cancel') },
      ).then(async () => { await applyRestore(cfg, envs) }).catch(() => { /* 取消 */ })
    } catch (e: any) {
      err.value = t('gc.restoreInvalid') + ': ' + String(e && e.message || e || '')
    }
  }
  r.readAsText(f)
  ;(ev.target as HTMLInputElement).value = ''
}
async function applyRestore(cfg: string, envs: Record<string, string>) {
  err.value = ''
  saving.value = true
  try {
    const j = await api('/config', { method: 'PUT', body: JSON.stringify({ yaml: cfg }) })
    if (Object.keys(envs).length) await applyEnv(envs)
    restartPending.value = j.restart_required || []
    savedOk.value = true
    ElMessage.success(t('gc.restoreOk'))
    await load()
  } catch (e: any) {
    const d = e?.body?.detail
    err.value = typeof d === 'string' ? d : (d ? JSON.stringify(d) : (e?.message || String(e)))
  } finally {
    saving.value = false
  }
}

async function applyEnv(map: Record<string, string>) {
  savingEnv.value = true
  try {
    const j = await api('/config/env', { method: 'POST', body: JSON.stringify({ envs: map }) })
    const envs = (j.envs || {}) as Record<string, any>
    envKeys.value = Object.keys(envs).map((env) => ({
      env,
      provider: envs[env].provider || '—',
      provider_name: envs[env].provider_name || '',
      set: !!envs[env].set,
      value: envs[env].value || '',
      show: false,
    }))
  } catch (e: any) {
    const d = e?.body?.detail
    throw new Error(typeof d === 'string' ? d : (d ? JSON.stringify(d) : (e?.message || String(e))))
  } finally {
    savingEnv.value = false
  }
}

function copyEnv(e: EnvRow) {
  if (!e.value) return
  copyText(e.value)
  ElMessage.success(t('gc.envCopied'))
}
async function saveEnv(e: EnvRow) {
  err.value = ''
  try {
    await applyEnv({ [e.env]: e.value })
    ElMessage.success(t('gc.envSaved'))
  } catch (e2: any) {
    err.value = e2?.message || String(e2)
  }
}

onMounted(load)
</script>

<style scoped>
.mt { margin-top: var(--gap); }
.wrap { flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.hidden { display: none; }
.hint { font-size: 11.5px; color: var(--muted-2); margin: 6px 0 2px; }
.restart-pill { margin-left: 6px; }
.editor {
  width: 100%;
  min-height: 58vh;
  box-sizing: border-box;
  resize: vertical;
  background: var(--chip);
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre;
}
.editor:focus { outline: none; border-color: var(--primary); }
.env-rt { display: flex; align-items: center; gap: 6px; min-width: 0; }
.env-in { flex: 0 0 260px; }
.op-row { display: flex; gap: 8px; margin-top: 14px; }
@media (max-width: 720px) {
  .env-rt { flex-wrap: wrap; }
  .env-in { flex: 1 1 100%; }
}
</style>