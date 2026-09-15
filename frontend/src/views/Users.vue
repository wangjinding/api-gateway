<template>
  <div class="page">
    <div class="card">
      <div class="card-h">
        <b>{{ t('settings.account') }}</b>
        <span class="sub">{{ users.length }} · {{ t('nav.subusers') }}</span>
        <div class="pager">
          <el-input v-model="q" size="small" class="search-inp" clearable :placeholder="t('common.search')">
            <template #prefix><Icon name="search" :size="14" /></template>
          </el-input>
          <el-button type="primary" size="small" @click="showCreate = true">
            <Icon name="plus" :size="14" />
            <span>{{ t('settings.createUser') }}</span>
          </el-button>
        </div>
      </div>

      <el-table
        v-if="users.length"
        :data="shown"
        size="default"
        row-key="name"
        :row-class-name="rowClass"
        :expand-row-keys="expanded"
        :empty-text="t('users.noData')"
        @expand-change="onExpand"
      >
        <el-table-column type="expand">
          <template #default="{ row }">
            <!-- 第十一轮 #2：admin 在此看该账号的调用日志 + 生成的图像/视频资产 -->
            <div class="expand-row" v-loading="act(row.name).loading">
              <el-tabs v-model="act(row.name).tab">
                <el-tab-pane :label="t('users.tabLogs')" name="logs">
                  <el-table :data="act(row.name).logs" size="small" max-height="360"
                    :empty-text="t('users.noData')">
                    <el-table-column :label="t('logs.time')" width="92">
                      <template #default="{ row: e }"><span class="code">{{ fmtClock(e.ts) }}</span></template>
                    </el-table-column>
                    <el-table-column :label="t('logs.result')" width="66">
                      <template #default="{ row: e }">
                        <el-tag :type="e.ok ? 'success' : 'danger'" size="small">{{ e.ok ? t('common.ok') : t('common.fail') }}</el-tag>
                      </template>
                    </el-table-column>
                    <el-table-column :label="t('logs.path')" min-width="160">
                      <template #default="{ row: e }"><span class="code">{{ e.method }} {{ e.path }}</span></template>
                    </el-table-column>
                    <el-table-column :label="t('logs.modelProvider')" min-width="140">
                      <template #default="{ row: e }">
                        <span class="code">{{ e.served || e.model || '—' }}</span>
                        <span v-if="e.provider" class="at-prov">@ {{ e.provider }}</span>
                      </template>
                    </el-table-column>
                    <el-table-column :label="t('logs.respContent')" min-width="220">
                      <template #default="{ row: e }">
                        <span v-if="e.response" class="ellip" :title="e.response">{{ e.response }}</span>
                        <span v-else-if="!e.ok" class="err-text ellip" :title="e.error">{{ e.error || '—' }}</span>
                        <span v-else class="muted">—</span>
                      </template>
                    </el-table-column>
                  </el-table>
                </el-tab-pane>

                <el-tab-pane :label="`${t('users.tabImages')} (${act(row.name).images.length})`" name="images">
                  <div v-if="act(row.name).images.length" class="asset-grid">
                    <div v-for="im in act(row.name).images" :key="im.id" class="asset-cell">
                      <el-image :src="im.src" fit="cover" class="asset-thumb"
                        :preview-src-list="act(row.name).images.map((x: any) => x.src).filter(Boolean)" :initial-index="0" preview-teleported />
                      <div class="asset-cap ellip3" :title="im.prompt">{{ im.prompt || '—' }}</div>
                      <div class="asset-meta code ellip">{{ im.served || im.model || '' }} · {{ fmtClock((im.ts || 0) / 1000) }}</div>
                      <!-- 管理员代下载他人资产：产物在跨域 CDN 上，直链 <a download> 不生效，
                           统一走后端 /v1/media/download 代拉，与图像/视频页同一个 lib/download.ts。 -->
                      <el-button size="small" class="asset-dl"
                        :loading="dlKey === 'i-' + im.id"
                        @click="dlAsset(im.src, im.prompt, 'image', 'i-' + im.id)">
                        <Icon name="dl" :size="14" />{{ t('users.dl') }}
                      </el-button>
                    </div>
                  </div>
                  <el-empty v-else :image-size="60" :description="t('users.noData')" />
                </el-tab-pane>

                <el-tab-pane :label="`${t('users.tabVideos')} (${act(row.name).videos.length})`" name="videos">
                  <div v-if="act(row.name).videos.length" class="asset-grid">
                    <div v-for="vk in act(row.name).videos" :key="vk.id" class="asset-cell">
                      <video v-if="vk.src" :src="vk.src" class="asset-thumb" controls preload="metadata" />
                      <div v-else class="asset-thumb video-ph">
                        <el-tag :type="vk.kind === 'fail' ? 'danger' : vk.kind === 'done' ? 'success' : 'info'" size="small">{{ vk.status || vk.kind }}</el-tag>
                      </div>
                      <div class="asset-cap ellip3" :title="vk.prompt">{{ vk.prompt || '—' }}</div>
                      <div class="asset-meta code ellip">{{ vk.model || '' }} · {{ vk.elapsed ? vk.elapsed + 's' : '' }}</div>
                      <el-button v-if="vk.src" size="small" class="asset-dl"
                        :loading="dlKey === 'v-' + vk.id"
                        @click="dlAsset(vk.src, vk.prompt, 'video', 'v-' + vk.id)">
                        <Icon name="dl" :size="14" />{{ t('users.dl') }}
                      </el-button>
                    </div>
                  </div>
                  <el-empty v-else :image-size="60" :description="t('users.noData')" />
                </el-tab-pane>
              </el-tabs>
            </div>
          </template>
        </el-table-column>

        <el-table-column :label="t('settings.userCol')" :min-width="isMobile ? 120 : 150">
          <template #default="{ row }">
            <div class="acc">
              <span class="avatar" :class="{ alt: row.name !== store.user }">{{ initial(row.name) }}</span>
              <span class="acc-nm">{{ row.name }}</span>
              <!-- 第二十四轮: 停用状态贴在账号名后（桌面/手机都看得见），悬停显示停用时间 -->
              <span v-if="row.disabled" class="pill warn" :title="row.disabled_at ? t('settings.userDisabledAt', { ts: fmtTs(row.disabled_at) }) : ''">
                {{ t('settings.userDisabled') }}
              </span>
            </div>
            <!-- 第十八轮: 手机上把「角色 / Key 数 / 创建时间」三列收进账号单元格，
                 否则五列总宽 937px 在 390 屏上必须横滑，操作列根本看不到。 -->
            <div v-if="isMobile" class="acc-sub">
              <span class="pill" :class="row.role === 'admin' ? 'ok' : 'mute'">{{ row.role }}</span>
              <span class="nowrap muted">{{ row.api_key_count }} / {{ keyLimitText(row.role) }} {{ t('settings.keyCount') }}</span>
              <span v-if="atKeyLimit(row)" class="pill warn">{{ t('settings.keyLimitFull') }}</span>
              <span class="nowrap muted">{{ fmtTs(row.created) }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column v-if="!isMobile" :label="t('settings.roleCol')" width="104">
          <template #default="{ row }">
            <span class="pill" :class="row.role === 'admin' ? 'ok' : 'mute'">{{ row.role }}</span>
          </template>
        </el-table-column>

        <!-- Key 数：admin 不限，普通账号与后端 MAX_KEYS_PER_MEMBER 一致（3 把） -->
        <el-table-column v-if="!isMobile" :label="t('settings.keyCount')" width="136">
          <template #default="{ row }">
            <span class="nowrap">{{ row.api_key_count }} / {{ keyLimitText(row.role) }}</span>
            <span v-if="atKeyLimit(row)" class="pill warn">{{ t('settings.keyLimitFull') }}</span>
          </template>
        </el-table-column>

        <el-table-column v-if="!isMobile" :label="t('settings.userCreated')" width="150">
          <template #default="{ row }"><span class="nowrap muted">{{ fmtTs(row.created) }}</span></template>
        </el-table-column>

        <el-table-column :min-width="isMobile ? 152 : 320" align="right">
          <!-- 第十八轮: 手机上操作列改图标按钮（文字版四颗要 ~320px，必然被挤出屏外） -->
          <template #default="{ row }">
            <div v-if="isMobile" class="ops">
              <el-tooltip :content="t('settings.resetPw')" placement="top">
                <el-button text type="primary" size="small" @click="resetPw(row.name)"><Icon name="key" :size="16" /></el-button>
              </el-tooltip>
              <!-- 管理员账号不可停用（唯一管理员被停用就没人能进管理页），故 admin 不给这颗按钮 -->
              <el-tooltip v-if="row.role !== 'admin'" :content="row.disabled ? t('settings.enableUser') : t('settings.disableUser')" placement="top">
                <el-button text :type="row.disabled ? 'success' : 'warning'" size="small" @click="toggleDisabled(row)">
                  <Icon :name="row.disabled ? 'play' : 'ban'" :size="16" />
                </el-button>
              </el-tooltip>
              <el-tooltip :content="t('users.viewActivity')" placement="top">
                <el-button text size="small" @click="toggleActivity(row)"><Icon name="log" :size="16" /></el-button>
              </el-tooltip>
              <el-tooltip :content="t('settings.revokeKeys')" placement="top">
                <el-button text type="warning" size="small" :disabled="!row.api_key_count" @click="revokeKeys(row.name)"><Icon name="stop" :size="16" /></el-button>
              </el-tooltip>
              <el-tooltip v-if="row.name !== store.user" :content="t('common.delete')" placement="top">
                <el-button text type="danger" size="small" @click="remove(row.name)"><Icon name="trash" :size="16" /></el-button>
              </el-tooltip>
            </div>
            <template v-else>
              <el-button text type="primary" size="small" @click="resetPw(row.name)">{{ t('settings.resetPw') }}</el-button>
              <!-- 第二十四轮: 停用/启用。停用后该账号不能登录、名下 API Key 一并失效，
                   所以停用要二次确认；启用是无风险操作，点了就生效 -->
              <el-button v-if="row.role !== 'admin'" text :type="row.disabled ? 'success' : 'warning'" size="small"
                @click="toggleDisabled(row)">
                {{ row.disabled ? t('settings.enableUser') : t('settings.disableUser') }}
              </el-button>
              <el-button text size="small" @click="toggleActivity(row)">{{ t('users.viewActivity') }}</el-button>
              <el-button text type="warning" size="small" :disabled="!row.api_key_count" @click="revokeKeys(row.name)">
                {{ t('settings.revokeKeys') }}
              </el-button>
              <el-button v-if="row.name !== store.user" text type="danger" size="small" @click="remove(row.name)">
                {{ t('common.delete') }}
              </el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else :description="t('common.none')" :image-size="80" />
    </div>

    <!-- 新建账号：账号名 + 可选初始密码（与旧版行内输入等价，只是收进弹层） -->
    <el-dialog v-model="showCreate" :title="t('settings.createUser')" width="420px">
      <div class="field">
        <label>{{ t('settings.newUserName') }}</label>
        <el-input v-model="newUser" :placeholder="t('settings.newUserName')" clearable @keyup.enter="create" />
      </div>
      <div class="field dialog-f2">
        <label>{{ t('settings.userPwOptional') }}</label>
        <el-input v-model="newPw" type="password" show-password clearable
          :placeholder="t('settings.userPwOptional')" @keyup.enter="create" />
      </div>
      <div class="hint">{{ t('settings.newUserTip') }}</div>
      <template #footer>
        <el-button size="small" @click="showCreate = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" size="small" :loading="creating" @click="create">{{ t('settings.createUser') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, errText } from '../api'
import Icon from '../components/Icon.vue'
import { downloadMedia, nameFromPrompt, stamp } from '../lib/download'
import { useIsMobile } from '../lib/ui'
import { useSessionStore } from '../store/session'

const { t } = useI18n()
const store = useSessionStore()
// 第十八轮: 窄屏把「角色/Key 数/创建时间」三列折进账号单元格、操作列改图标按钮，
//   目的是让这张表在 390 宽下不用横滑（此前列总宽 937px，操作列在屏外）。
const { isMobile } = useIsMobile()
const users = ref<any[]>([])
const newUser = ref('')
const newPw = ref('')
const creating = ref(false)
const showCreate = ref(false)
const q = ref('')

// 与 app/users.py 的 MAX_KEYS_PER_MEMBER / key_limit_for_role 对齐，仅用于展示 n / 3
const MEMBER_KEY_LIMIT = 3

// 停用的账号整行压暗一点（一眼能看出哪些不能用了），但内容照常可读 —— 不是隐藏
function rowClass({ row }: any): string {
  return row?.disabled ? 'row-off' : ''
}

function keyLimitText(role: string): string {
  return role === 'admin' ? '∞' : String(MEMBER_KEY_LIMIT)
}
function atKeyLimit(row: any): boolean {
  return row.role !== 'admin' && Number(row.api_key_count || 0) >= MEMBER_KEY_LIMIT
}
function initial(name: string): string {
  return String(name || '?').slice(0, 1).toUpperCase()
}

// ── 资产下载（管理员代下载他人生成的图/视频）──
// 与图像页/视频页同一条路：产物在跨域 CDN 上（无 CORS 头、<a download> 对跨域无效），
// 所以走后端 /v1/media/download 代拉再落盘；文件名用「提示词前缀 + 时间戳」。
const dlKey = ref<string>('')
async function dlAsset(src: string, prompt: string, kind: 'image' | 'video', key: string) {
  if (!src) { ElMessage.error(t('users.dlNoSrc')); return }
  dlKey.value = key
  try {
    await downloadMedia(src, `${nameFromPrompt(prompt, kind)}-${stamp()}`)
    ElMessage.success(t('users.dlOk'))
  } catch (e: any) {
    ElMessage.error(t('users.dlFail', { msg: e?.message || String(e) }))
  } finally {
    dlKey.value = ''
  }
}

// 搜索只过滤本页已加载的账号列表（不发新请求）
const shown = computed(() => {
  const s = q.value.trim().toLowerCase()
  if (!s) return users.value
  return users.value.filter((u) => String(u.name || '').toLowerCase().includes(s))
})

// 每个账号的活动数据（日志 + 图像 + 视频），展开时懒加载一次
type Activity = { loading: boolean; loaded: boolean; tab: string; logs: any[]; images: any[]; videos: any[] }
const activity = reactive<Record<string, Activity>>({})
function act(name: string): Activity {
  if (!activity[name]) {
    activity[name] = { loading: false, loaded: false, tab: 'logs', logs: [], images: [], videos: [] }
  }
  return activity[name]
}
const expanded = ref<string[]>([])

function fmtTs(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
function fmtClock(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function err(e: any) {
  return errText(e)   // 统一从 {error:{message}} / {detail} 两种信封里取原因（见 api.ts）
}

async function ask(text: string) {
  try {
    await ElMessageBox.confirm(text, t('settings.confirm'), { type: 'warning' })
    return true
  } catch { return false }
}

async function loadActivity(name: string) {
  const a = act(name)
  if (a.loaded) return
  a.loading = true
  try {
    const [lg, im, vd] = await Promise.all([
      api(`/auth/users/${encodeURIComponent(name)}/logs?n=200`),
      api(`/auth/users/${encodeURIComponent(name)}/history/images`),
      api(`/auth/users/${encodeURIComponent(name)}/history/videos`),
    ])
    a.logs = lg.entries || []
    a.images = im.items || []
    a.videos = vd.items || []
    a.loaded = true
  } catch (e: any) {
    ElMessage.error(err(e))
  } finally { a.loading = false }
}

async function onExpand(row: any, expandedRows: any) {
  const isOpen = Array.isArray(expandedRows) ? expandedRows.includes(row) : !!expandedRows
  expanded.value = isOpen ? [row.name] : expanded.value.filter((k) => k !== row.name)
  if (isOpen) await loadActivity(row.name)
}

// 行内「查看活动」按钮：复用同一个展开回调（展开态仍由 expanded 驱动）
function toggleActivity(row: any) {
  onExpand(row, !expanded.value.includes(row.name))
}

async function load() {
  try { users.value = (await api('/auth/users')).users || [] } catch (e: any) { ElMessage.error(err(e)) }
}

async function create() {
  const name = newUser.value.trim()
  if (!name) return
  creating.value = true
  try {
    const body: any = { username: name }
    if (newPw.value) body.password = newPw.value
    await api('/auth/users', { method: 'POST', body: JSON.stringify(body) })
    ElMessage.success(t('settings.createUser') + ': ' + name)
    newUser.value = ''
    newPw.value = ''
    showCreate.value = false
    await load()
  } catch (e: any) { ElMessage.error(err(e)) }
  finally { creating.value = false }
}

async function resetPw(name: string) {
  if (!await ask(t('settings.confirmResetPw', { name }))) return
  try {
    const j = await api(`/auth/users/${name}/password`, { method: 'POST', body: JSON.stringify({}) })
    ElMessage.success(t('settings.resetPwOk', { name, n: j.revoked_sessions ?? 0 }))
    await load()
  } catch (e: any) { ElMessage.error(err(e)) }
}

// 第二十四轮: 停用 / 启用账号（PUT /auth/users/{name}/disabled）
//   停用后该账号 ①不能登录（密码正确也拒，服务端回 403 说明原因）；②名下 API Key 立即失效
//   （Key 记录不删，重新启用即恢复）；③已登录会话被立即踢掉。故停用要二次确认。
async function toggleDisabled(row: any) {
  const name = String(row.name)
  const off = !row.disabled            // off=true 表示本次要「停用」
  if (off && !await ask(t('settings.confirmDisableUser', { name }))) return
  try {
    const j = await api(`/auth/users/${encodeURIComponent(name)}/disabled`, {
      method: 'PUT', body: JSON.stringify({ disabled: off }),
    })
    if (j.disabled) ElMessage.success(t('settings.userDisabledOk', { name, n: j.revoked_sessions ?? 0 }))
    else ElMessage.success(t('settings.userEnabledOk', { name }))
    await load()
  } catch (e: any) { ElMessage.error(err(e)) }
}

async function revokeKeys(name: string) {
  if (!await ask(t('settings.confirmRevokeKeys', { name }))) return
  try {
    const j = await api(`/auth/users/${name}/apikeys`, { method: 'DELETE' })
    ElMessage.success(t('settings.revokeKeysOk', { name, n: j.revoked_keys ?? 0 }))
    await load()
  } catch (e: any) { ElMessage.error(err(e)) }
}

async function remove(name: string) {
  if (!await ask(t('settings.confirmDeleteUser', { name }))) return
  try {
    const j = await api(`/auth/users/${name}`, { method: 'DELETE' })
    ElMessage.success(t('settings.accountDeleted', { name, n: j.revoked_sessions ?? 0 }))
    delete activity[name]
    await load()
  } catch (e: any) { ElMessage.error(err(e)) }
}

onMounted(load)
</script>

<style scoped>
.search-inp { width: 180px; }
.acc { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.acc-nm { color: var(--text); }
/* 第二十四轮: 停用账号的名字压暗（状态靠旁边的「已停用」胶囊点明） */
.row-off :deep(.acc-nm) { color: var(--muted-2); }
/* 第十八轮: 手机端账号单元格的第二行（角色 / Key 数 / 创建时间） */
.acc-sub { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 5px; font-size: 11.5px; }
/* 手机端操作列：图标按钮一行排开，去掉 Element 相邻按钮的 12px 左边距。
   第二十四轮加「停用/启用」后这里变成 5 颗图标 —— 留 flex-wrap：万一某台机器上按钮宽
   超出操作列（152px）就换行，绝不让表格被撑宽、把操作列挤出屏外（第十八轮的老毛病）。 */
.ops { display: flex; align-items: center; gap: 2px; row-gap: 2px; flex-wrap: wrap; justify-content: flex-end; }
.ops :deep(.el-button + .el-button) { margin-left: 0; }
.ops :deep(.el-button) { padding: 6px; }
.at-prov { color: var(--muted-2); margin-left: 4px; font-size: 11px; }
.err-text { color: var(--err); }
.ellip { display: inline-block; max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; }
.ellip3 {
  font-size: 12px; color: var(--text); line-height: 1.4;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden; word-break: break-all;
}
.asset-grid { display: flex; flex-wrap: wrap; gap: 12px; }
.asset-cell { width: 150px; }
.asset-thumb {
  width: 150px; height: 110px; border-radius: 8px; display: block;
  background: var(--chip); border: 1px solid var(--line); object-fit: cover;
}
.video-ph { display: flex; align-items: center; justify-content: center; }
.asset-cap { margin-top: 4px; }
.asset-meta { color: var(--muted-2); margin-top: 2px; }
/* 下载按钮放卡片底部整行（与图像/视频页一致），不做缩略图上的浮层：手机端不会挤变形 */
.asset-dl { width: 100%; margin-top: 6px; }
.dialog-f2 { margin-top: 12px; }
.dialog-f2 + .hint { margin-top: 10px; }

@media (max-width: 768px) {
  .search-inp { width: 100%; }
}
</style>
