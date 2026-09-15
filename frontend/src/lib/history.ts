// 服务端历史（图像 / 视频 / 对话）读写封装。
//
// 为什么要这一层：三类历史原先只存在浏览器里，图像/视频在 localStorage、对话在
// sessionStorage —— 换设备看不见、关标签页就丢，而且 `setItem` 抛异常（配额写满）
// 被 `catch {}` 静默吞掉，用户看到的就是"我生成过的东西没了"。
// 现在以服务端 data/history.json 为准（按账号隔离、按 id upsert），本地只留一份缓存。
//
// 约定：**所有失败都要冒泡给调用方去提示**，不再静默丢数据。
import { api } from '../api'

export type HistoryKind = 'chat' | 'images' | 'videos'

/** 拉取本账号某类别的历史；后端不可用时返回 null（调用方回落到本地缓存）。 */
export async function pullHistory(kind: HistoryKind): Promise<any[] | null> {
  try {
    const j = await api(`/history/${kind}`)
    return Array.isArray(j.items) ? j.items : []
  } catch {
    return null
  }
}

/** 按 id upsert 提交（整份或增量都行）；返回 false 表示没存上，调用方要提示。 */
export async function pushHistory(kind: HistoryKind, items: any[]): Promise<boolean> {
  try {
    await api(`/history/${kind}`, { method: 'POST', body: JSON.stringify({ items }) })
    return true
  } catch {
    return false
  }
}

export async function deleteHistoryItem(kind: HistoryKind, id: string | number): Promise<boolean> {
  try {
    await api(`/history/${kind}/${encodeURIComponent(String(id))}`, { method: 'DELETE' })
    return true
  } catch {
    return false
  }
}

export async function clearHistory(kind: HistoryKind): Promise<boolean> {
  try {
    await api(`/history/${kind}`, { method: 'DELETE' })
    return true
  } catch {
    return false
  }
}

/** 给没有 id 的旧本地数据补一个稳定 id（迁移用，别让 upsert 把它们当新条目反复插）。 */
export function ensureIds(items: any[], prefix: string): any[] {
  return items.map((it, i) => (it && it.id !== undefined && it.id !== null
    ? it
    : { ...it, id: `${prefix}-${i}-${it?.ts ?? it?.started ?? it?.created ?? Date.now()}` }))
}
