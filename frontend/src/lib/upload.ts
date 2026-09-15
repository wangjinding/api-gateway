// 第十三轮: 页面端「上传图片 / 音频 → data URI」工具。
//
// 为什么不上传到服务端再拿公网 URL？
//   Agnes 要求素材能被它公开访问，但实测它同样接受 `data:image/png;base64,...` /
//   `data:audio/wav;base64,...`（图像生成、视频 keyframe/reference、对话视觉三条路径都跑通过）。
//   浏览器端直接转 data URI 是最短路径：不落盘、不占公网带宽、也不用新增上传端点与临时文件清理。
//
// 但**必须压缩**：网关注入体上限 2MB（server.max_body_size），Nginx 也是 client_max_body_size 2m，
// 而 base64 会把体积再放大 4/3。手机随手拍的 3~5MB 照片直接转 base64 只会换来一个 413。
// 所以这里按用途分级压缩 + 逐级降档，并在提交前做「整包体积」预算检查。

/** 网关 Nginx / 应用层的请求体上限（字节），留一点余量给 prompt 与 JSON 结构。 */
export const BODY_BUDGET = 1_700_000

export interface MediaPreset {
  /** 长边上限（像素） */
  maxSide: number
  /** JPEG 质量 */
  quality: number
  /** 单件 data URI 目标上限（字节） */
  maxBytes: number
  label: string
}

/** 各用途的压缩档位。数值来自实测：1024px/q0.82 的普通照片约 150~300KB（base64 后 ×1.37）。 */
export const PRESETS: Record<string, MediaPreset> = {
  // 对话视觉：给模型看的，1024px 足够（再大也只会烧 token）
  chat: { maxSide: 1024, quality: 0.8, maxBytes: 500_000, label: '对话附图' },
  // 图生图 / 多图合成：保留构图细节
  ref: { maxSide: 1024, quality: 0.85, maxBytes: 500_000, label: '参考图' },
  // 视频首/尾帧：画面直接进视频，给到 1280px
  frame: { maxSide: 1280, quality: 0.85, maxBytes: 450_000, label: '首/尾帧' },
  // 视频参考图：最多 5 张，单件必须小（5 × 300KB ≈ 1.5MB，逼近 2MB 上限）
  videoRef: { maxSide: 1024, quality: 0.8, maxBytes: 300_000, label: '视频参考图' },
  // 视频参考音频：无浏览器端压缩手段，只能卡体积（最多 3 段）
  audio: { maxSide: 0, quality: 0, maxBytes: 420_000, label: '参考音频' },
  // 第二十二轮 意见反馈附图：一条最多 3 张，管理员要在电脑上看清细节，给到 1280px；
  // 但整包（3 张 + 缩略图）必须落在网关/Nginx 的 2MB 上限内，所以单张卡 400KB。
  feedback: { maxSide: 1280, quality: 0.8, maxBytes: 400_000, label: '反馈图片' },
}

export interface PickedMedia {
  /** 原始文件名（用于卡片/标签展示） */
  name: string
  kind: 'image' | 'audio'
  /** 直接塞进请求体的 data URI */
  uri: string
  /** 压成缩略图后的 data URI（对话历史只存这个，避免把历史文件撑爆） */
  thumb: string
  /** data URI 的字节数（base64 长度换算） */
  bytes: number
  /** 压缩后的像素尺寸（图片才有） */
  width?: number
  height?: number
  /** 是否做过有损压缩（用于提示用户"已压缩"） */
  compressed: boolean
}

const AUDIO_EXT = ['mp3', 'wav', 'm4a', 'aac', 'ogg', 'oga', 'flac', 'opus', 'webm']

/** base64 data URI 的实际字节数（4 个 base64 字符 = 3 字节）。 */
export function dataUriBytes(uri: string): number {
  const i = uri.indexOf(',')
  if (i < 0) return uri.length
  const b64 = uri.slice(i + 1)
  const pad = b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0
  return Math.max(0, Math.floor((b64.length * 3) / 4) - pad)
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n}B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)}KB`
  return `${(n / 1024 / 1024).toFixed(2)}MB`
}

function extOf(name: string): string {
  return (name.split('.').pop() || '').toLowerCase()
}

export function isAudioFile(f: File): boolean {
  return (f.type || '').startsWith('audio/') || AUDIO_EXT.includes(extOf(f.name))
}

export function isImageFile(f: File): boolean {
  if ((f.type || '').startsWith('image/')) return true
  return ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'].includes(extOf(f.name))
}

function readAsDataUri(f: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const fr = new FileReader()
    fr.onerror = () => reject(new Error('读取文件失败'))
    fr.onload = () => resolve(String(fr.result || ''))
    fr.readAsDataURL(f)
  })
}

async function loadBitmap(file: File): Promise<{ w: number; h: number; draw: CanvasImageSource; dispose: () => void }> {
  // 优先 createImageBitmap（快、不占 DOM）；老浏览器回落到 <img> + objectURL
  if (typeof createImageBitmap === 'function') {
    try {
      const bmp = await createImageBitmap(file)
      return { w: bmp.width, h: bmp.height, draw: bmp, dispose: () => bmp.close && bmp.close() }
    } catch { /* 落到 <img> 分支（部分浏览器对某些格式不支持） */ }
  }
  const url = URL.createObjectURL(file)
  try {
    const img = await new Promise<HTMLImageElement>((resolve, reject) => {
      const el = new Image()
      el.onload = () => resolve(el)
      el.onerror = () => reject(new Error('图片解码失败（格式不支持？）'))
      el.src = url
    })
    return {
      w: img.naturalWidth, h: img.naturalHeight, draw: img,
      dispose: () => URL.revokeObjectURL(url),
    }
  } catch (e) {
    URL.revokeObjectURL(url)
    throw e
  }
}

async function encodeCanvas(file: File, maxSide: number, quality: number): Promise<{
  uri: string; width: number; height: number
}> {
  const src = await loadBitmap(file)
  try {
    const scale = Math.min(1, maxSide / Math.max(src.w || 1, src.h || 1))
    const w = Math.max(1, Math.round((src.w || 1) * scale))
    const h = Math.max(1, Math.round((src.h || 1) * scale))
    const cv = document.createElement('canvas')
    cv.width = w
    cv.height = h
    const ctx = cv.getContext('2d')
    if (!ctx) throw new Error('浏览器不支持 Canvas 2D，无法压缩图片')
    // 白底：PNG 透明区转 JPEG 会变黑，铺一层白更接近原图观感
    ctx.fillStyle = '#ffffff'
    ctx.fillRect(0, 0, w, h)
    ctx.drawImage(src.draw, 0, 0, w, h)
    return { uri: cv.toDataURL('image/jpeg', quality), width: w, height: h }
  } finally {
    src.dispose()
  }
}

/** 生成缩略图（对话历史、列表预览用）。失败时回落到原图，不阻断主流程。 */
export async function makeThumb(dataUri: string, maxSide = 256, quality = 0.6): Promise<string> {
  try {
    const blob = await (await fetch(dataUri)).blob()
    const file = new File([blob], 'thumb.jpg', { type: blob.type || 'image/jpeg' })
    return (await encodeCanvas(file, maxSide, quality)).uri
  } catch {
    return dataUri
  }
}

/**
 * 图片 → data URI（自动压缩到预设档位内）。
 *
 * 策略：文件本来就不大且是无损格式 → 原样用；否则画布重编码，并按
 * 「原档 → 0.75×边长 → 0.5×边长」逐级降档，直到落进 maxBytes 预算。
 */
export async function pickImage(file: File, preset: MediaPreset): Promise<PickedMedia> {
  if (!isImageFile(file)) throw new Error(`${file.name} 不是图片文件`)
  const raw = await readAsDataUri(file)
  // 小体积 PNG/WEBP 直接原样用（避免无谓的有损压缩，也保住透明通道）
  if (raw.length <= preset.maxBytes && /png|webp|gif|bmp/i.test(file.type || extOf(file.name))) {
    return {
      name: file.name, kind: 'image', uri: raw, thumb: await makeThumb(raw),
      bytes: raw.length, compressed: false,
    }
  }
  const tries: Array<[number, number]> = [
    [preset.maxSide, preset.quality],
    [Math.round(preset.maxSide * 0.75), Math.max(0.5, preset.quality - 0.1)],
    [Math.round(preset.maxSide * 0.5), 0.6],
    [Math.round(preset.maxSide * 0.35), 0.5],
  ]
  let last = ''
  for (const [side, q] of tries) {
    const enc = await encodeCanvas(file, side, q)
    last = enc.uri
    if (enc.uri.length <= preset.maxBytes) {
      return {
        name: file.name, kind: 'image', uri: enc.uri, thumb: await makeThumb(enc.uri),
        bytes: enc.uri.length, width: enc.width, height: enc.height, compressed: true,
      }
    }
  }
  throw new Error(`${file.name} 压缩后仍有 ${formatBytes(dataUriBytes(last))}，超过 ${preset.label} 单件上限 ${formatBytes(preset.maxBytes)}`)
}

/** 音频 → data URI（浏览器端无法重编码，只做体积与格式校验）。 */
export async function pickAudio(file: File, preset: MediaPreset = PRESETS.audio): Promise<PickedMedia> {
  if (!isAudioFile(file)) throw new Error(`${file.name} 不是音频文件（支持 ${AUDIO_EXT.join('/')}）`)
  if (file.size > preset.maxBytes) {
    throw new Error(`${file.name} 有 ${formatBytes(file.size)}，超过参考音频上限 ${formatBytes(preset.maxBytes)}（请裁剪或压缩后再传）`)
  }
  const uri = await readAsDataUri(file)
  return { name: file.name, kind: 'audio', uri, thumb: '', bytes: uri.length, compressed: false }
}

/** 按类型自动选择解析方式。 */
export async function pickMedia(file: File, preset: MediaPreset): Promise<PickedMedia> {
  return isAudioFile(file) ? pickAudio(file, PRESETS.audio) : pickImage(file, preset)
}

/**
 * 提交前的整包预算检查：素材 + 提示词必须落在网关/Nginx 的请求体上限内。
 * 抛出可读错误（页面直接展示），而不是让浏览器拿到一个 413。
 */
export function assertBodyBudget(parts: Array<string | undefined>, extraBytes = 0): void {
  const total = parts.reduce((n, p) => n + (p ? p.length : 0), 0) + extraBytes
  if (total > BODY_BUDGET) {
    throw new Error(
      `本次请求体约 ${formatBytes(total)}，超过网关上送上限（约 ${formatBytes(BODY_BUDGET)}）。` +
      '请减少素材数量，或换体积更小的图片/音频。',
    )
  }
}
