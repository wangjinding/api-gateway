// M3: 媒体（图像/视频）通用工具。

/** 从图像 data 项里解析可显示的 src（url / base64 / data:URI 均可）。 */
export function imageSrc(item: any): string {
  if (!item) return ''
  if (item.url) return item.url
  if (item.b64_json) return 'data:image/png;base64,' + item.b64_json
  if (item.data_url) return item.data_url
  if (typeof item.data === 'string') {
    if (item.data.startsWith('data:')) return item.data
    return 'data:image/png;base64,' + item.data
  }
  return ''
}

function looksLikeVideo(s: string): boolean {
  return /\.(mp4|webm|ogg|m3u8|mov)(\?|#|$)/i.test(s)
}

/** 从视频轮询返回的 raw 对象里尽量找出视频播放地址（按常见键优先，再递归兜底）。 */
export function findVideoUrl(obj: any): string {
  if (!obj) return ''
  if (typeof obj === 'string') return looksLikeVideo(obj) ? obj : ''
  if (Array.isArray(obj)) {
    for (const o of obj) { const u = findVideoUrl(o); if (u) return u }
    return ''
  }
  if (typeof obj === 'object') {
    for (const k of ['video_result', 'video_url', 'play_url', 'url', 'html_url', 'video', 'media_url', 'result', 'data', 'mp4']) {
      if (k in obj) { const u = findVideoUrl(obj[k]); if (u) return u }
    }
    for (const v of Object.values(obj)) { const u = findVideoUrl(v); if (u) return u }
  }
  return ''
}

/** 状态文案归类（completed/succeeded=成功；failed/failed=失败；其余为等待）。 */
export function videoStatusKind(s: string): 'done' | 'fail' | 'pending' {
  if (!s) return 'pending'
  const t = String(s).toLowerCase()
  if (['completed', 'succeeded', 'success'].includes(t)) return 'done'
  if (['failed', 'fail', 'error'].includes(t)) return 'fail'
  return 'pending'
}