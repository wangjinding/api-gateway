<template>
  <!--
    处理记录时间线（第二十二轮追加）：提交 + 每一次状态变更，old → new 逐条列出。
    提交人和管理员看到的是**同一串记录**（服务端 history 原样下发），不各算各的。

    超过 LIMIT 条时默认只显示最后几条（最新的处理动作最重要），点开看全部。
  -->
  <div v-if="steps.length" class="tl">
    <div class="tl-h">
      <span>{{ t('fbLog.title') }}</span>
      <span class="cnt">{{ steps.length }}</span>
    </div>
    <ol class="tl-list">
      <li v-for="(s, i) in shown" :key="i" class="tl-i" :class="s.to">
        <span class="dot" />
        <!-- 第一格是「提交」（没有上一个状态）；后面每格是「改到哪个状态 ← 原来是什么」 -->
        <span class="lbl">{{ s.from ? t(`fbStatus.${s.to}`) : t('fbLog.submitted') }}</span>
        <span v-if="s.from && s.from !== s.to" class="from">← {{ t(`fbStatus.${s.from}`) }}</span>
        <span v-if="s.by" class="who">{{ s.by }}</span>
        <span class="spacer" />
        <span class="ts">{{ fmt(s.ts) }}</span>
        <div v-if="s.note" class="note">{{ s.note }}</div>
      </li>
    </ol>
    <button v-if="steps.length > LIMIT" class="more" type="button" @click="expanded = !expanded">
      {{ expanded ? t('fbLog.less') : t('fbLog.more', { n: steps.length }) }}
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { FeedbackItem } from '../lib/feedback'

const props = defineProps<{ item: FeedbackItem }>()
const { t } = useI18n()

const LIMIT = 4          // 默认最多显示 4 条（再长就折叠）
const expanded = ref(false)

const steps = computed(() => props.item.history || [])
const shown = computed(() => (expanded.value ? steps.value : steps.value.slice(-LIMIT)))

function fmt(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
</script>

<style scoped>
.tl { margin-top: 12px; }
.tl-h { display: flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--muted); }
.tl-h .cnt {
  min-width: 16px; padding: 0 4px; border-radius: 6px; text-align: center;
  background: var(--chip); color: var(--muted); font-size: 10.5px; font-variant-numeric: tabular-nums;
}
.tl-list { list-style: none; margin: 10px 0 0; padding: 0 0 0 2px; }
.tl-i {
  position: relative; display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px;
  padding: 0 0 15px 20px; font-size: 11.5px; color: var(--muted); line-height: 1.5;
}
/* 竖线串起每一步：最后一条不再往下画 */
.tl-i::before {
  content: ''; position: absolute; left: 5px; top: 7px; bottom: 0;
  width: 1px; background: var(--line);
}
.tl-i:last-child::before { display: none; }
.dot {
  position: absolute; left: 2px; top: 5px; width: 7px; height: 7px; border-radius: 50%;
  background: var(--muted-2);
}
.tl-i.pending .dot { background: var(--warn, #d99b3a); }
.tl-i.doing .dot { background: var(--primary); }
.tl-i.done .dot { background: var(--ok, #3aa76d); }
.tl-i .lbl { color: var(--text); font-weight: 600; }
.tl-i .from { color: var(--muted-2); }
.tl-i .who {
  padding: 0 5px; border-radius: 6px; background: var(--chip); color: var(--muted); font-size: 10.5px;
}
.tl-i .spacer { flex: 1; }
.tl-i .ts { color: var(--muted-2); font-size: 10.5px; font-variant-numeric: tabular-nums; }
/* 说明：左侧主题色竖线，和正文区分开（"官方回复了"一眼可见） */
.tl-i .note {
  flex: 1 0 100%; margin-top: 10px; padding: 8px 12px;
  border-left: 2px solid var(--primary); background: var(--primary-soft);
  border-radius: 0 8px 8px 0; color: var(--text); font-size: 11.5px;
  line-height: 1.62; word-break: break-word; white-space: pre-wrap;
}
.more {
  margin: 0 0 2px 16px; padding: 0; border: 0; background: none; cursor: pointer;
  color: var(--primary); font-size: 11.5px;
}
.more:hover { text-decoration: underline; }
</style>
