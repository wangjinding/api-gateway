<template>
  <!-- 登录页（第四次重做，方案一「极简白卡」，老板 2026-09-13 拍板）：
       浅底 + 单列居中白卡，靠留白、柔光和极淡的几何纹理撑气质。
       与上一版的区别：去掉「顶部通栏背景图 + 左品牌区 + 右表单」的分栏卡片结构
       （老板认为不好看），也去掉自绘 SVG 背景图 —— 现在整页装饰全是 CSS 图层（见下方样式）。
       内容仍只有：logo + 品牌标题 + 用户名/密码 + 登录按钮。 -->
  <div class="login-wrap">
    <!-- 装饰层（从下到上：底色 → 柔光 → 卡片背后光雾 → 点阵肌理 → 右下同心环），
         全部 pointer-events:none，不参与交互、不引任何外链资源 -->
    <div class="wash" aria-hidden="true" />
    <div class="aurora" aria-hidden="true" />
    <div class="dots" aria-hidden="true" />
    <div class="rings" aria-hidden="true"><i /><i /><i /></div>

    <!-- 中英切换：页面右上角白玻璃胶囊（桌面 / 窄屏同一套配色，靠 surface token 自动适配三主题） -->
    <el-dropdown class="lang-top" @command="onLang">
      <span class="lang">{{ locale === 'en' ? 'EN' : '中' }}</span>
      <template #dropdown>
        <el-dropdown-menu>
          <el-dropdown-item command="zh" :disabled="locale === 'zh'">中文</el-dropdown-item>
          <el-dropdown-item command="en" :disabled="locale === 'en'">English</el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>

    <main class="card">
      <span class="logo"><Icon name="bolt" :size="22" /></span>
      <h1>{{ t('login.title') }}</h1>

      <el-alert v-if="err" :title="err" type="error" class="login-err" show-icon :closable="false" />

      <form @submit.prevent="doLogin">
        <div class="field">
          <label>{{ t('login.userPlaceholder') }}</label>
          <el-input v-model="username" size="large" autocomplete="username" @keyup.enter="doLogin" />
        </div>
        <div class="field">
          <label>{{ t('login.passPlaceholder') }}</label>
          <el-input
            v-model="password"
            type="password"
            size="large"
            show-password
            autocomplete="current-password"
            @keyup.enter="doLogin"
          />
        </div>
        <el-button type="primary" size="large" class="submit" :loading="loading" native-type="submit">
          {{ t('login.btn') }}
        </el-button>
      </form>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { useSessionStore } from '../store/session'
import { setLocale } from '../i18n'
import Icon from '../components/Icon.vue'

const router = useRouter()
const store = useSessionStore()
const { t, locale } = useI18n()
const username = ref('')
const password = ref('')
const loading = ref(false)
const err = ref('')

function onLang(l: string) { setLocale(l as any) }

async function doLogin() {
  err.value = ''
  if (!username.value.trim() || !password.value) { err.value = t('login.errEmpty'); return }
  loading.value = true
  try {
    await store.login(username.value.trim(), password.value)
    ElMessage.success(t('login.success'))
    router.push('/dashboard')
  } catch (e: any) {
    err.value = authErr(e)
  } finally {
    loading.value = false
  }
}

function authErr(e: any): string {
  if (e && e.status === 401) return t('login.err401')
  if (e && e.status === 429) return t('login.err429')
  // 403：服务端带得出明确原因（第二十四轮的「账号已被停用，请联系管理员」就走这条），
  // 优先显示原文 —— 比页面这句笼统的「账号被禁用或角色异常」有用得多；拿不到才退回文案。
  if (e && e.status === 403) return String(e?.message || '') || t('login.err403')
  if (e && e.message) return String(e.message)
  return t('login.errConn')
}
</script>

<style scoped>
/* 颜色口径：能用 token 的地方一律用 token（上面这套装饰层因此能在 A/B/C 三主题下自动换色）；
   需要"半透明的主题色"时用 color-mix(in srgb, var(--x) N%, transparent)，不再写死
   rgba(79,99,245,…) 这种只对单一主题成立的常量。color-mix 在 Chrome 111+ / Safari 16.2+
   可用，不支持时该层退化为透明（少一层柔光，不影响可读性与布局）。 */
.login-wrap {
  min-height: 100vh; min-height: 100dvh;
  display: flex; align-items: center; justify-content: center;
  padding: 40px 24px; background: var(--bg); position: relative;
}

/* ① 柔光：左上主色、右下青调，让纯色底有体积感（不抢卡片，所以透明度很低） */
.wash {
  position: fixed; inset: 0; pointer-events: none; background:
    radial-gradient(58% 48% at 8% -6%, color-mix(in srgb, var(--primary) 13%, transparent), transparent 62%),
    radial-gradient(46% 42% at 100% 108%, color-mix(in srgb, var(--chart-b) 12%, transparent), transparent 64%);
}
/* ② 卡片背后的一团主色光雾：白卡浮在光雾上才有厚度（挪掉不丑，只是页面会显平） */
.aurora {
  position: fixed; left: 50%; top: 47%; width: 920px; height: 560px;
  transform: translate(-50%, -50%); pointer-events: none; filter: blur(14px);
  background: radial-gradient(50% 50% at 50% 50%, color-mix(in srgb, var(--primary) 17%, transparent), transparent 72%);
}
/* ③ 点阵肌理：只在左上角渐显（mask 化开），几乎察觉不到但页面不发死 */
.dots {
  position: fixed; inset: 0; pointer-events: none;
  background-image: radial-gradient(color-mix(in srgb, var(--text) 16%, transparent) 1px, transparent 1px);
  background-size: 24px 24px;
  -webkit-mask-image: radial-gradient(52% 46% at 14% 8%, #000, transparent 72%);
  mask-image: radial-gradient(52% 46% at 14% 8%, #000, transparent 72%);
}
/* ④ 右下同心环：三个同心细线圆，越外越淡，给卡片一个视觉落点 */
.rings i { position: fixed; border-radius: 50%; pointer-events: none; border: 1px solid color-mix(in srgb, var(--primary) 14%, transparent); }
.rings i:nth-child(1) { width: 340px; height: 340px; right: -110px; bottom: -120px; }
.rings i:nth-child(2) { width: 540px; height: 540px; right: -210px; bottom: -220px; border-color: color-mix(in srgb, var(--primary) 10%, transparent); }
.rings i:nth-child(3) { width: 760px; height: 760px; right: -320px; bottom: -330px; border-color: color-mix(in srgb, var(--chart-b) 10%, transparent); }

/* 右上角中英切换：白玻璃胶囊（窄屏同款，颜色全走 token） */
.lang-top { display: block; position: fixed; top: 24px; right: 26px; z-index: 5; }
.lang {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 46px; padding: 6px 14px; border-radius: 999px; cursor: pointer; outline: none;
  background: color-mix(in srgb, var(--surface) 74%, transparent);
  border: 1px solid var(--line); color: var(--muted); font-size: 12.5px; line-height: 1.4;
  box-shadow: 0 2px 8px color-mix(in srgb, var(--text) 8%, transparent);
  backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
}

/* 卡片：两层阴影（token 的基础层 + 一层更深的落地层），压在柔光/光雾上要能跳出来 */
.card {
  position: relative; z-index: 2; width: 440px; max-width: 100%;
  background: var(--surface); border: 1px solid var(--line); border-radius: 22px;
  padding: 42px 40px 40px;
  box-shadow: var(--shadow), 0 24px 50px -26px rgba(0, 0, 0, .28);
}
.logo {
  width: 46px; height: 46px; border-radius: 14px; display: grid; place-items: center;
  color: var(--primary-ink);
  /* 主色自身上下渐变：A/C 主题换了主色，这块自动跟着换（不用为每套主题写死色值） */
  background: linear-gradient(142deg,
    color-mix(in srgb, var(--primary) 84%, #fff), var(--primary) 55%,
    color-mix(in srgb, var(--primary) 82%, #000));
  box-shadow: 0 10px 22px -8px color-mix(in srgb, var(--primary) 62%, transparent);
}
.card h1 { margin: 20px 0 28px; font-size: 21px; font-weight: 650; letter-spacing: .1px; color: var(--text); }
.login-err { margin-bottom: 16px; border-radius: 12px; }
.field { margin-bottom: 16px; }
.field label { display: block; font-size: 12.5px; color: var(--muted); margin-bottom: 8px; }

/* 输入框：Element Plus 的 wrapper 本身就是「1px 内阴影当边框」，所以覆盖它而不是加 border，
   否则会出现双边框。填充式（chip 底）比描边式更松弛，也更好压住圆角。 */
.field :deep(.el-input__wrapper) {
  height: 48px; padding: 0 15px; border-radius: 12px; background: var(--chip);
  box-shadow: 0 0 0 1px var(--line) inset;
  transition: background .16s, box-shadow .16s;
}
.field :deep(.el-input__wrapper:hover) {
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--primary) 34%, var(--line)) inset;
}
/* 聚焦态必须带 !important：全局 style.css 里有一条
   `.el-input__wrapper.is-focus { box-shadow: 0 0 0 1px var(--primary) inset !important }`
   （给全站输入框统一"细边"聚焦样式），不用 !important 的话这里的「1px 主色 + 3.5px 柔光」
   双环会被它整条盖掉，只剩 1px 细边。作用域仍只限登录页，不影响其它页面。 */
.field :deep(.el-input__wrapper.is-focus) {
  background: var(--surface);
  box-shadow: 0 0 0 1px var(--primary) inset, 0 0 0 3.5px var(--primary-soft) !important;
}
.field :deep(.el-input__inner) { font-size: 14.5px; color: var(--text); }
.field :deep(.el-input__inner::placeholder) { color: var(--muted-2); }

.submit {
  width: 100%; height: 48px; margin-top: 12px; border-radius: 12px;
  font-size: 15px; font-weight: 600;
  box-shadow: 0 10px 22px -10px color-mix(in srgb, var(--primary) 75%, transparent);
}

/* 手机端：卡片化整为零 —— 去掉卡片的底/边/影，表单直接在页面柔光底上居中，
   下方同心环缩一档（窄屏里 760px 的环几乎全在屏幕外，留着只是白算）
   （旧版那套「顶部色块 + 上浮卡片 + 接缝」的结构整段删掉了） */
@media (max-width: 600px) {
  .login-wrap { align-items: stretch; padding: 0; }
  .card {
    width: 100%; border: 0; border-radius: 0; box-shadow: none; background: transparent;
    padding: 104px 22px 40px; display: flex; flex-direction: column; justify-content: center;
  }
  .rings i:nth-child(1) { width: 260px; height: 260px; right: -130px; bottom: -90px; }
  .rings i:nth-child(2) { width: 400px; height: 400px; right: -200px; bottom: -160px; }
  .rings i:nth-child(3) { display: none; }
  .lang-top { top: 18px; right: 18px; }
}
</style>
