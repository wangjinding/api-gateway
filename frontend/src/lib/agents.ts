// 智能体（第三十八轮 · 路线 A）—— 接口封装 + SSE 解析。
//
// 设计见 docs/agents-dev-plan.md；SSE 协议见 docs/qwenpaw-agent-api-sse.md。
// 后端把 QwenPaw 的事件翻译成这几种（前端不必懂 AgentScope）：

import { getToken } from '../api'

export interface AgentItem {
  slug: string
  display_name: string
  description: string
  params?: { temperature?: number; max_tokens?: number; top_p?: number; system_suffix?: string }
  engine?: { type: string; base_url: string; agent_id: string }
  access?: { roles: string[]; users: string[] }
  status: 'draft' | 'published' | 'retired'
  created_at?: string
  updated_at?: string
  access_scope?: string
  last_used_at?: string
  run_total?: number
  engine_model?: string
  engine_ready?: boolean
}

export interface AgentListResp {
  agents: AgentItem[]
  max_agents: number
  active: number
  engine: { model: string; ready: boolean; base_url: string; agent_id: string }
}

export interface AgentRun {
  at: string
  account: string
  rounds: number
  tools: number
  tokens: number
  ms: number
  status: string
}

/** 后端 SSE 的事件类型。 */
export type AgentEvent =
  | { type: 'start'; slug: string; session_id: string }
  | { type: 'text'; text: string }
  | { type: 'step'; id: string; name?: string; args?: any; status: 'running' | 'done'; ms?: number; output?: any }
  | { type: 'usage'; prompt: number; completion: number; cached: number; model: string; provider: string; context_ratio?: number }
  | { type: 'done'; status: string }
  | { type: 'error'; message: string }

function authHeaders(): Record<string, string> {
  return { Authorization: `Bearer ${getToken()}` }
}

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const r = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...(init.headers || {}) },
  })
  const body = await r.json().catch(() => null)
  if (!r.ok) {
    const msg = body?.error?.message || body?.detail || `HTTP ${r.status}`
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
  }
  return body as T
}

export const listAgents = (includeRetired = false) =>
  req<AgentListResp>(`/agents${includeRetired ? '?include_retired=true' : ''}`)

export const createAgent = (body: Record<string, any>) =>
  req<{ status: string; agent: AgentItem }>('/agents', { method: 'POST', body: JSON.stringify(body) })

export const updateAgent = (slug: string, patch: Record<string, any>) =>
  req<{ status: string; agent: AgentItem }>(`/agents/${slug}`, { method: 'PUT', body: JSON.stringify(patch) })

export const retireAgent = (slug: string) =>
  req<{ status: string }>(`/agents/${slug}`, { method: 'DELETE' })

export const restoreAgent = (slug: string) =>
  req<{ status: string; agent: AgentItem }>(`/agents/${slug}/restore`, { method: 'POST' })

export const agentRuns = (slug: string) =>
  req<{ slug: string; total: number; last_used_at: string; items: AgentRun[] }>(`/agents/${slug}/runs`)

export const testAgent = (slug: string) =>
  req<{ ok: boolean; ms: number; detail: string }>(`/agents/${slug}/test`, { method: 'POST' })

export const agentSessions = (slug: string) =>
  req<{ slug: string; kind: string; items: any[] }>(`/agents/${slug}/sessions`)

export const stopAgent = (slug: string, chatId: string) =>
  req<{ status: string }>(`/agents/${slug}/stop?chat_id=${encodeURIComponent(chatId)}`, { method: 'POST' })

/**
 * 与智能体对话（SSE 流式）。逐事件回调 —— 与 Chat.vue 的流式处理同款思路。
 *
 * 返回一个 abort 函数（停止按钮用）。注意：网关侧的"停止"是转发给引擎的
 * /console/chat/stop；前端 abort 只是断开本地连接，两者可同时用。
 */
export function streamAgentChat(
  slug: string,
  messages: { role: string; content: string }[],
  sessionId: string,
  onEvent: (ev: AgentEvent) => void,
): () => void {
  const ctrl = new AbortController()
  ;(async () => {
    try {
      const r = await fetch(`/agents/${slug}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ messages, session_id: sessionId }),
        signal: ctrl.signal,
      })
      if (!r.ok) {
        const body = await r.json().catch(() => null)
        const msg = body?.error?.message || body?.detail || `HTTP ${r.status}`
        onEvent({ type: 'error', message: String(msg) })
        return
      }
      const reader = r.body!.getReader()
      const dec = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        const parts = buf.split('\n\n')
        buf = parts.pop() || ''
        for (const part of parts) {
          const line = part.trim()
          if (!line.startsWith('data:')) continue
          try {
            onEvent(JSON.parse(line.slice(5).trim()) as AgentEvent)
          } catch { /* 忽略坏帧 */ }
        }
      }
    } catch (e: any) {
      if (e?.name !== 'AbortError') onEvent({ type: 'error', message: String(e?.message || e) })
    }
  })()
  return () => ctrl.abort()
}
