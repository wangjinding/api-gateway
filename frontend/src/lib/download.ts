// M6: 生成图片/视频的「下载」。
//
// 为什么要绕后端一圈（而不是 `<a :href="src" download>`）：
//   生成产物挂在第三方 CDN 上（如 cos-platform-outputs.agnes-ai.cn），该 CDN
//   **不返回 CORS 头**，于是浏览器里两条直链下载的路都堵死：
//     1) fetch(src) → blob 被跨域拦（拿不到字节，自己造不了 Blob 下载）；
//     2) `<a href="跨域 URL" download>`：HTML 规范里 download 属性对跨域地址无效 ——
//        点下去只会"新标签页打开"，不是存文件（手机端最明显）。
//   所以走同源的 /v1/media/download，由网关代拉并补 Content-Disposition，
//   前端拿 blob 自己触发保存。只有"要不要下载"是前端的事，"能不能拉到"交给后端。

import { getToken } from '../api'

/** 服务端代下载端点（与网关同源）。 */
const DL_PATH = '/v1/media/download'

/** 从 Content-Disposition 里取文件名（优先 RFC 5987 的 filename*，它支持中文）。 */
export function filenameFromHeader(cd: string): string {
  if (!cd) return ''
  const star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(cd)
  if (star && star[1]) {
    try { return decodeURIComponent(star[1].trim().replace(/^"|"$/g, '')) } catch { /* 落下面兜底 */ }
  }
  const plain = /filename\s*=\s*"([^"]*)"/i.exec(cd) || /filename\s*=\s*([^;]+)/i.exec(cd)
  return plain && plain[1] ? plain[1].trim() : ''
}

/**
 * data:URI → Blob（不经过 fetch）。
 *
 * 为什么不用 `fetch(dataURI)`：CSP 是 `connect-src 'self'`，data: 不在白名单里，
 * fetch 会被浏览器直接拦掉（前端只看到「下载失败」）。解码 base64 属于纯计算，
 * 不触网，也就不受 CSP 约束 —— 顺便离线也能用。
 */
export function dataUriToBlob(src: string): Blob {
  const m = /^data:([^,]*),([\s\S]*)$/i.exec(src)
  if (!m) throw new Error('bad data URI')
  const header = m[1] || ''
  const body = m[2] || ''
  const semi = header.indexOf(';')
  const mime = (semi === -1 ? header : header.slice(0, semi)) || 'application/octet-stream'
  if (/;base64/i.test(header)) {
    const bin = atob(body.replace(/\s+/g, ''))
    const bytes = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
    return new Blob([bytes], { type: mime })
  }
  try {
    return new Blob([decodeURIComponent(body)], { type: mime })
  } catch {
    return new Blob([body], { type: mime })   // 百分号残缺就按原样存，别把下载搞崩
  }
}

/** data:URI 的 MIME → 扩展名（本地/上游直回 base64 的图走这条路）。 */
function dataExt(src: string): string {
  const m = /^data:([a-z0-9.+/-]+)/i.exec(src)
  const map: Record<string, string> = {
    'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp', 'image/gif': '.gif',
    'video/mp4': '.mp4', 'video/webm': '.webm', 'video/quicktime': '.mov',
  }
  return (m && map[m[1].toLowerCase()]) || ''
}

/** 兜底文件名：从 URL 末段取（带 query 的地址会退化到 `media-<时间戳>`）。 */
function fallbackName(src: string, prefix: string): string {
  const ext = dataExt(src)
  if (ext) return `${prefix}-${stamp()}${ext}`
  const tail = (src.split('?')[0] || '').split('#')[0].split('/').pop() || ''
  if (tail && /\.[a-z0-9]{2,5}$/i.test(tail)) return tail.slice(-120)
  return `${prefix}-${stamp()}.bin`
}

/** yyyyMMdd-HHmm（本地时区），用来区分同名下载。 */
export function stamp(d = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`
}

/** 用提示词做文件名前缀：先截断再抹掉斜杠等路径成分（避免落盘时被当目录）。 */
export function nameFromPrompt(prompt: string, fallback = 'media', max = 24): string {
  const s = (prompt || '').replace(/[\r\n\t]+/g, ' ').replace(/[\\/:*?"<>|]+/g, ' ').trim()
  const cut = s.slice(0, max).trim()
  return cut || fallback
}

/** 触发浏览器保存一个 blob（同源 blob: URL，download 属性一定生效）。 */
function saveBlob(blob: Blob, name: string): void {
  const href = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = href
  a.download = name || 'download'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
  // 立刻 revoke 在部分浏览器（Safari/Firefox）会打断尚未开始的保存，延后回收。
  setTimeout(() => URL.revokeObjectURL(href), 10_000)
}

/**
 * 下载一个生成出来的图片/视频。
 *
 * @param src      产物地址：http(s)（走后端代拉）或 data:（浏览器内直接存）
 * @param name     期望文件名（不含后缀也行，后端按 Content-Type 补）；
 *                 实际落盘名以服务端 Content-Disposition 为准
 * @throws Error   带上后端给的人话原因（白名单/失效/超限…），交给调用方弹提示
 */
export async function downloadMedia(src: string, name = ''): Promise<void> {
  if (!src) throw new Error('empty media src')

  if (src.startsWith('data:')) {
    // data URI 不出网、也不该绕后端：本地解码成 blob 直接存（离线也能下载；
    // 注意不能用 fetch —— CSP 的 connect-src 'self' 会把它拦掉，见 dataUriToBlob）
    const base = name || `media-${stamp()}`
    saveBlob(dataUriToBlob(src), base.includes('.') ? base : base + dataExt(src))
    return
  }

  const q = new URLSearchParams({ url: src })
  if (name) q.set('filename', name)
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`

  const resp = await fetch(`${DL_PATH}?${q.toString()}`, { headers })
  if (!resp.ok) {
    // 网关错误统一是 {"error":{"message": "..."}}（见 openai_compat_error）
    let msg = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      msg = (body && (body.error?.message || body.detail)) || msg
    } catch { /* 非 JSON（如代理层 502 页面）就用状态码 */ }
    if (resp.status === 401) {
      window.dispatchEvent(new Event('gw-auth-expired'))  // 会话过期 → 回登录页
    }
    throw new Error(msg)
  }

  const serverName = filenameFromHeader(resp.headers.get('content-disposition') || '')
  const blob = await resp.blob()
  saveBlob(blob, serverName || fallbackName(src, name || 'media'))
}
