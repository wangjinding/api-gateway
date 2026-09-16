<template>
  <div class="page">
    <el-alert v-if="err" :title="err" type="error" show-icon :closable="false" class="mb" />

    <div class="set-grid">
      <!-- 外观与语言：三张主题卡 + 跟随系统 + 语言 -->
      <div class="card">
        <div class="card-h">
          <b>{{ t('settings.appearance') }}</b>
          <span class="sub">{{ t('settings.themeSub') }}</span>
        </div>

        <div
          v-for="id in THEME_IDS"
          :key="id"
          class="theme-card"
          :class="{ on: theme === id }"
          @click="pickTheme(id)"
        >
          <div class="theme-prev">
            <span class="l" :style="{ background: THEME_META[id].side, borderRight: '1px solid var(--line)' }" />
            <span class="r" :style="{ background: THEME_META[id].canvas }">
              <i :style="{ background: THEME_META[id].accent, width: '70%' }" />
              <i :style="{ background: THEME_META[id].accent, opacity: .35 }" />
              <i :style="{ background: THEME_META[id].accent, opacity: .2, width: '60%' }" />
            </span>
          </div>
          <div class="flex-1">
            <b>{{ t(THEME_META[id].nameKey) }}</b>
            <div class="hint">{{ t(THEME_META[id].descKey) }}</div>
          </div>
          <span v-if="theme === id" class="pill ok">{{ t('settings.current') }}</span>
          <span v-else class="chip-sel">{{ t('settings.switch') }}</span>
        </div>

        <div class="set-row">
          <div>
            <div class="lb">{{ t('settings.followSystem') }}</div>
            <div class="sub">{{ t('settings.followSub') }}</div>
          </div>
          <div class="rt">
            <el-switch :model-value="followSystem" @change="onFollow" />
          </div>
        </div>

        <div class="set-row">
          <div>
            <div class="lb">{{ t('settings.language') }}</div>
            <div class="sub">{{ t('settings.langSub') }}</div>
          </div>
          <div class="rt">
            <el-radio-group :model-value="locale" size="small" @change="onLocale">
              <el-radio-button value="zh">中文</el-radio-button>
              <el-radio-button value="en">English</el-radio-button>
            </el-radio-group>
          </div>
        </div>
      </div>

      <!-- 生成默认值：新建对话时的初始参数（Chat.vue 读同一份 localStorage） -->
      <div class="card">
        <div class="card-h">
          <b>{{ t('settings.chatDefaults') }}</b>
          <span class="sub">{{ t('settings.defsTip') }}</span>
        </div>

        <div class="set-row">
          <div class="lb">{{ t('settings.defaultModel') }}</div>
          <div class="rt">
            <el-select v-model="defs.model" size="small" clearable :placeholder="t('chat.routeGroup')" style="width: 210px">
              <el-option v-for="m in textModels" :key="m" :label="m" :value="m" />
            </el-select>
          </div>
        </div>
        <div class="set-row">
          <div class="lb">{{ t('settings.temperature') }}</div>
          <div class="rt">
            <el-input-number v-model="defs.temperature" size="small" :min="0" :max="2" :step="0.1" controls-position="right" style="width: 130px" />
          </div>
        </div>
        <div class="set-row">
          <div class="lb">{{ t('settings.topP') }}</div>
          <div class="rt">
            <el-input-number v-model="defs.top_p" size="small" :min="0" :max="1" :step="0.05" controls-position="right" style="width: 130px" />
          </div>
        </div>
        <div class="set-row">
          <div class="lb">{{ t('settings.maxTokens') }}</div>
          <div class="rt">
            <el-input-number v-model="defs.max_tokens" size="small" :min="1" :max="200000" :step="256" controls-position="right" style="width: 150px" />
          </div>
        </div>
        <div class="set-row">
          <div class="lb">{{ t('settings.streaming') }}</div>
          <div class="rt">
            <el-switch v-model="defs.stream" />
          </div>
        </div>
        <div class="set-row" style="align-items: flex-start">
          <div class="lb flex-1">
            {{ t('settings.system') }}
            <el-input v-model="defs.system" type="textarea" :rows="2" class="mt-8" :placeholder="t('chat.system')" />
          </div>
        </div>

        <div class="op-row">
          <el-button type="primary" size="small" :loading="savingDefs" @click="saveDefs">{{ t('settings.save') }}</el-button>
          <el-button size="small" @click="resetDefs">{{ t('common.reset') }}</el-button>
        </div>
      </div>
    </div>

    <div class="set-grid mt">
      <!-- 账号与安全 -->
      <div class="card">
        <div class="card-h">
          <b>{{ t('settings.account') }}</b>
          <span class="sub">{{ store.user }}<template v-if="store.isAdmin"> · {{ t('common.admin') }}</template></span>
        </div>

        <div class="set-row">
          <div class="flex-1">
            <div class="lb">{{ t('settings.changePw') }}</div>
            <div class="sub">{{ t('settings.pwTip') }}</div>
          </div>
          <div class="rt">
            <el-input
              v-model="pw.new"
              type="password"
              show-password
              size="small"
              style="width: 150px"
              :placeholder="t('settings.newPw')"
              @keyup.enter="savePw"
            />
            <el-button type="primary" size="small" :loading="pw.saving" @click="savePw">{{ t('common.save') }}</el-button>
          </div>
        </div>

        <div class="set-row">
          <div class="flex-1">
            <div class="lb">{{ t('settings.myKeys') }}</div>
            <div class="sub">{{ t('settings.myKeysSub') }}</div>
          </div>
          <div class="rt">
            <el-button size="small" @click="router.push('/keys')">{{ t('settings.manage') }}</el-button>
          </div>
        </div>

        <div class="set-row">
          <div class="flex-1">
            <div class="lb">{{ t('common.logout') }}</div>
            <div class="sub">{{ t('settings.logoutTip') }}</div>
          </div>
          <div class="rt">
            <el-button size="small" type="danger" plain @click="doLogout">{{ t('settings.logoutBtn') }}</el-button>
          </div>
        </div>
      </div>

      <!-- 网关信息：只读快照。真要改网关参数走「网关配置」页（管理员，第二十三轮）。 -->
      <div class="card">
        <div class="card-h">
          <b>{{ t('settings.gwCard') }}</b>
        </div>
        <div class="set-row">
          <div class="lb">{{ t('settings.gwAddress') }}</div>
          <div class="rt code">{{ base }}</div>
        </div>
        <div class="set-row">
          <div class="lb">{{ t('settings.gwVersion') }}</div>
          <div class="rt">v{{ health.version || '-' }}</div>
        </div>
        <div class="set-row">
          <div class="lb">{{ t('settings.sessionLen') }}</div>
          <div class="rt">{{ store.expiresIn ? t('settings.hours', { n: Math.round(store.expiresIn / 3600) }) : '—' }}</div>
        </div>
        <div class="set-row">
          <div class="lb">{{ t('dashboard.inflight') }}</div>
          <div class="rt">{{ health.in_flight ?? '-' }} / {{ health.in_flight_peak ?? '-' }}</div>
        </div>
        <div class="set-row">
          <div class="lb">{{ t('dashboard.fallback') }}</div>
          <div class="rt">{{ health.fallback_total ?? 0 }}</div>
        </div>
        <div class="set-row" style="align-items: flex-start">
          <div class="lb">{{ t('settings.providerStatus') }}</div>
          <div class="rt" style="flex-wrap: wrap; justify-content: flex-end">
            <span v-for="p in providerRows" :key="p.name" class="pill" :class="p.cls">{{ p.name }} · {{ p.text }}</span>
          </div>
        </div>

        <!-- 第二十三轮：管理员从这里跳去改配置（普通账号看不到这个入口）；
             卡片本身也受 `GATEWAY_CONFIG_ENABLED` 管（老板要求暂时隐藏整块配置页）。 -->
        <div v-if="store.isAdmin && GATEWAY_CONFIG_ENABLED" class="set-row">
          <div class="flex-1">
            <div class="lb">{{ t('settings.gwConfig') }}</div>
            <div class="sub">{{ t('settings.gwConfigSub') }}</div>
          </div>
          <div class="rt">
            <el-button size="small" @click="router.push('/gateway-config')">{{ t('settings.gwConfigBtn') }}</el-button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { GATEWAY_CONFIG_ENABLED } from '../lib/features'
import { useI18n } from 'vue-i18n'
import { computed } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { useSessionStore } from '../store/session'
import { setLocale } from '../i18n'
import { getChatDefaults, setChatDefaults, type ChatDefaults } from '../lib/settings'
import { useTheme, THEME_IDS, THEME_META, type ThemeId } from '../lib/theme'

const router = useRouter()
const store = useSessionStore()
const base = window.location.origin
const health = ref<any>({})
const err = ref('')

const { t, locale } = useI18n()
const { theme, followSystem, setTheme, setFollowSystem } = useTheme()

const TEXT_MODELS = ['wjd', 'agnes-3.0-flash', 'ark-code-latest', 'qwen3.8-flash', 'GLM-4.7-Flash', 'GLM-4-Flash-250414', 'GLM-Z1-Flash', 'GLM-4.6V-Flash', 'GLM-4V-Flash', 'GLM-4.1V-Thinking-Flash']
const textModels = ref<string[]>(TEXT_MODELS)

function pickTheme(id: ThemeId) {
  setTheme(id)
  ElMessage.success(t('theme.applied') + ' · ' + t(THEME_META[id].nameKey))
}
function onFollow(v: string | number | boolean) { setFollowSystem(!!v) }
function onLocale(l: string) { setLocale(l as 'zh' | 'en') }

// ── 生成默认值 ──────────────────────────────────────────────
const defs = reactive<ChatDefaults>({ temperature: 0.7, top_p: 1, max_tokens: null, stream: true, system: '' })
const savingDefs = ref(false)

function loadDefs() {
  const d = getChatDefaults()
  defs.model = d.model ?? 'wjd'
  defs.temperature = d.temperature ?? 0.7
  defs.top_p = d.top_p ?? 1
  defs.max_tokens = d.max_tokens ?? null
  defs.stream = d.stream !== false
  defs.system = d.system ?? ''
}
function saveDefs() {
  savingDefs.value = true
  try {
    setChatDefaults({ ...defs })
    ElMessage.success(t('settings.saveOk'))
  } finally {
    savingDefs.value = false
  }
}
function resetDefs() {
  setChatDefaults({})
  loadDefs()
  ElMessage.success(t('settings.resetOk'))
}

// ── 账号 ────────────────────────────────────────────────────
const pw = reactive({ new: '', saving: false })

// 已登录会话直接设新密码（老板要求免输旧密码），改完所有会话失效
async function savePw() {
  if (!pw.new) { ElMessage.warning(t('settings.pwNeedNew')); return }
  pw.saving = true
  try {
    await api('/auth/password', { method: 'POST', body: JSON.stringify({ new_password: pw.new }) })
    ElMessage.success(t('settings.pwChanged'))
    pw.new = ''
    await store.logout()
    router.push('/login')
  } catch (e: any) {
    ElMessage.error(e?.body?.detail || (e && e.message) || String(e))
  } finally {
    pw.saving = false
  }
}

async function doLogout() {
  await store.logout()
  router.push('/login')
}

const providerRows = computed(() => {
  const p = health.value.providers || {}
  return Object.keys(p).map((k) => {
    const c = String(p[k].circuit || '').toUpperCase()
    const cls = c === 'CLOSED' ? 'ok' : c === 'HALF_OPEN' ? 'warn' : c === 'OPEN' ? 'err' : 'mute'
    return { name: p[k].name, cls, text: t(`dashboard.circ_${String(p[k].circuit || '').toLowerCase() || ''}`) }
  })
})

onMounted(async () => {
  loadDefs()
  try {
    health.value = await api('/health')
    // 默认模型下拉用真实模型列表（拿不到就退回内置候选）
    try {
      const um = await api('/v1/models')
      const list = (um.data || []).filter((x: any) => x.type !== 'route').map((x: any) => x.id)
      if (list.length) textModels.value = ['wjd', ...list]
    } catch { /* 模型列表可选 */ }
  } catch (e: any) {
    if (e && e.status === 401) { router.push('/login'); return }
    err.value = t('settings.loadErr') + ': ' + ((e && e.message) || String(e))
  }
})
</script>

<style scoped>
.mt { margin-top: var(--gap); }
.mt-8 { margin-top: 8px; }
.op-row { display: flex; gap: 8px; margin-top: 14px; }
</style>
