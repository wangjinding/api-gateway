<template>
  <!-- 反馈附图：缩略图内嵌（服务端 JSON 里带着），点开才去取原图 —— 列表页因此零额外请求。 -->
  <div v-if="item.images && item.images.length" class="fb-imgs">
    <button
      v-for="(im, i) in item.images"
      :key="i"
      class="fb-th"
      type="button"
      :title="im.name || t('inbox.preview')"
      @click="open(i)"
    >
      <img v-if="im.thumb" :src="im.thumb" :alt="im.name || 'feedback image'" />
      <span v-else class="ph"><Icon name="img" :size="16" /></span>
    </button>
  </div>

  <el-dialog v-model="show" :title="title" width="min(860px, 92vw)">
    <div class="fb-full">
      <div v-if="loading" class="hint">{{ t('inbox.loading') }}</div>
      <img v-else-if="full" :src="full" alt="feedback image" />
      <div v-else class="hint">{{ t('inbox.loadFail') }}</div>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from './Icon.vue'
import { feedbackImage, type FeedbackItem } from '../lib/feedback'

const props = defineProps<{ item: FeedbackItem }>()
const { t } = useI18n()

const show = ref(false)
const loading = ref(false)
const full = ref('')
const title = ref('')

/** 同一张图重复点开时复用已取到的 data URI，别反复要同一份数据。 */
const cache = new Map<number, string>()

async function open(idx: number) {
  const im = props.item.images[idx]
  const kb = im && im.bytes ? `${Math.round(im.bytes / 1024)}KB` : ''
  title.value = [im?.name || t('inbox.preview'), kb].filter(Boolean).join(' · ')
  show.value = true
  if (cache.has(idx)) {
    full.value = cache.get(idx) as string
    return
  }
  loading.value = true
  full.value = ''
  try {
    const uri = await feedbackImage(props.item.id, idx)
    cache.set(idx, uri)
    full.value = uri
  } catch {
    full.value = ''
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.fb-imgs { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.fb-th {
  width: 84px; height: 84px; padding: 0; border: 1px solid var(--line);
  border-radius: 10px; overflow: hidden; background: var(--surface-2); cursor: zoom-in;
}
.fb-th img { width: 100%; height: 100%; object-fit: cover; display: block; }
.fb-th .ph { display: grid; place-items: center; width: 100%; height: 100%; color: var(--muted-2); }
.fb-full { display: grid; place-items: center; min-height: 120px; }
.fb-full img { max-width: 100%; max-height: 68vh; border-radius: 10px; display: block; }
</style>
