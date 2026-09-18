// M2: 对话 Playground —— 流式 / 非流式调用网关 /v1/chat/completions。
// 同源请求，带会话 Bearer；流式用 fetch + body reader 直接解析 SSE。

import { getToken } from '../api'

export interface Meta {
  served: string
  provider: string
  requested: string
}

export interface Usage {
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  images?: string[]             // 第十一轮 #3：随该条用户消息附带的图片（URL 或 data URI）
  imgNames?: string[]           // 第十三轮：图片文件名（展示用，data URI 看不出来源）
  imgThumbs?: string[]          // 第十三轮：缩略图 data URI —— 落服务端历史时用它替代原图
                                //   （服务端单条 512KB，原图 base64 会被截断成坏图）
  reasoning?: string          // 推理内容（reasoning_content），单独折叠展示
  meta?: Meta                  // 网关元信息（served/provider）
  usage?: Usage
  cost?: number                // estimated_cost_cny（仅非流式）
  streaming?: boolean           // 正在流式输出中
  error?: string
}

let idSeq = 1
export function nextId(): number { return idSeq++ }
export function seekId(maxId: number): void { idSeq = maxId + 1 }

export interface HistItem {
  role: string
  content: string
  images?: string[]
}

/** 组装给网关的 messages（system 前缀 + 历史 user/assistant 正文；推理不随历史发送）。
 *  带 images 的用户消息会发成 OpenAI 视觉格式：content=[{type:text},{type:image_url,...}]。 */
export function buildPayloadMessages(
  system: string,
  history: HistItem[],
): { role: string; content: any }[] {
  const out: { role: string; content: any }[] = []
  if (system && system.trim()) out.push({ role: 'system', content: system })
  for (const m of history) {
    const imgs = (m.images || []).filter(Boolean)
    if (!m.content && !imgs.length) continue
    if (imgs.length) {
      const parts: any[] = []
      if (m.content) parts.push({ type: 'text', text: m.content })
      for (const url of imgs) parts.push({ type: 'image_url', image_url: { url } })
      out.push({ role: m.role, content: parts })
    } else {
      out.push({ role: m.role, content: m.content })
    }
  }
  return out
}

/** 分块解析 SSE，回调每个 data 事件。返回被 [DONE] 或流结束决定的结果。 */
export async function streamChat(
  payload: Record<string, any>,
  onChunk: (chunk: any) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = getToken()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers['Authorization'] = `Bearer ${token}`
  const resp = await fetch('/v1/chat/completions', {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
    signal,
  })
  if (!resp.ok) {
    let body: any = null
    try { body = await resp.json() } catch { /* 不是 JSON */ }
    const err: any = new Error((body && (body.detail?.message || body.detail)) || `HTTP ${resp.status}`)
    err.status = resp.status
    err.body = body
    throw err
  }
  if (!resp.body) return
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) !== -1) {
      const frame = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data: ')) continue
        const data = line.slice(6)
        if (data.trim() === '[DONE]') return
        let chunk: any
        try { chunk = JSON.parse(data) } catch { continue }
        onChunk(chunk)
      }
    }
  }
}

/** 非流式：直接拿完整对象。 */
export async function chatOnce(payload: Record<string, any>): Promise<any> {
  const token = getToken()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers['Authorization'] = `Bearer ${token}`
  const resp = await fetch('/v1/chat/completions', {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
  })
  let body: any = null
  try { body = await resp.json() } catch { /* 忽略 */ }
  if (!resp.ok) {
    const err: any = new Error((body && (body.detail?.message || body.detail)) || `HTTP ${resp.status}`)
    err.status = resp.status
    err.body = body
    throw err
  }
  return body
}