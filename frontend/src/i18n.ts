// M5+: 多语言基础设施。默认跟随 localStorage 的 gw_locale，缺省中文；fallback 回中文。
import { createI18n } from 'vue-i18n'
import zh from './locales/zh'
import en from './locales/en'

export const LOCALE_KEY = 'gw_locale'
export type Locale = 'zh' | 'en'

export function getLocale(): Locale {
  const v = localStorage.getItem(LOCALE_KEY)
  return v === 'en' ? 'en' : 'zh'
}

const i18n = createI18n({
  legacy: false,
  globalInjection: true,
  locale: getLocale(),
  fallbackLocale: 'zh',
  messages: { zh, en },
})

export function setLocale(locale: Locale): void {
  i18n.global.locale.value = locale
  localStorage.setItem(LOCALE_KEY, locale)
  // 让 document 语言标签跟着变，便于 assistive tech / 字体
  document.documentElement.setAttribute('lang', locale)
}

// 初始化也要把语言标签写进 <html>：setLocale 只覆盖「手动切换」这条路径，
// 否则 localStorage 里存着 en 的老用户，<html lang> 会一直停在 index.html 里的 zh-CN
// （读屏软件会按中文念英文界面，字体回退也跟着走中文）。
if (typeof document !== 'undefined') document.documentElement.setAttribute('lang', getLocale())

export default i18n