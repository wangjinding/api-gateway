// 第二十二轮：意见反馈的前端契约层。
//
// 分工：服务端存「正文 + 缩略图」，原图按需取（见后端 app/feedback.py 的说明）。
// 这里只做三件事：提交、取自己的、取全部（admin），以及取某张原图。

import { api } from '../api'
import { PRESETS, assertBodyBudget, pickImage, type PickedMedia } from './upload'

/** 一条反馈最多几张图（与服务端 MAX_IMAGES 必须一致，改一处就得改两处）。 */
export const FEEDBACK_MAX_IMAGES = 3

/** 处理进度三档，顺序即流程顺序（与服务端 STATUSES 一致）。 */
export const FEEDBACK_STATUSES = ['pending', 'doing', 'done'] as const
export type FeedbackStatus = (typeof FEEDBACK_STATUSES)[number]
/** i18n 后缀：t(`fbStatus.${s}`) */
export const FEEDBACK_STATUS_CLASS: Record<FeedbackStatus, string> = {
  pending: 'o',  // 待处理：橙色，需要有人看一眼
  doing: 'p',    // 处理中：主题色
  done: 'g',     // 已处理：绿色
}

export interface FeedbackImage {
  name: string
  bytes: number
  /** 缩略图 data URI（服务端内嵌返回）；空串表示没有预览图 */
  thumb: string
  type?: string
}

/** 一条处理记录：谁（by）在什么时候（ts）把它从 from 改到 to，留了什么说明（note）。
 *  `from` 为空串表示「提交」那一步（没有前一个状态）。 */
export interface FeedbackStep {
  ts: number
  from: string
  to: FeedbackStatus
  note: string
  by: string
}

export interface FeedbackItem {
  id: string
  account: string
  content: string
  /** epoch 秒（与日志/用户页的时间口径一致） */
  created: number
  images: FeedbackImage[]
  /** 处理进度：pending / doing / done */
  status: FeedbackStatus
  /** 最新一条处理说明（提交人可见，可为空） */
  note: string
  /** 最后一次改状态的时间（epoch 秒） */
  updated: number
  /** 处理记录：提交 + 每一次状态变更，old → new，只增不改（服务端最多留 50 条） */
  history: FeedbackStep[]
  /** 管理员归档标记：收件箱「归档/取消归档」，不影响提交人视角 */
  archived?: boolean
}

export interface FeedbackDraft {
  content: string
  images: PickedMedia[]
}

/** 图片 → data URI（自动压缩到反馈档位）。缩略图由 pickImage 一并生成（256px，列表够用）。 */
export function pickFeedbackImage(file: File): Promise<PickedMedia> {
  return pickImage(file, PRESETS.feedback)
}

export async function submitFeedback(draft: FeedbackDraft): Promise<FeedbackItem> {
  const images = draft.images.map((m) => ({ uri: m.uri, thumb: m.thumb, name: m.name }))
  // 提交前的整包预算：宁可在这里给一句人话，也不要让浏览器吃到 Nginx 的 413
  assertBodyBudget([draft.content, ...images.flatMap((i) => [i.uri, i.thumb])])
  const r = await api('/feedback', {
    method: 'POST',
    body: JSON.stringify({ content: draft.content, images }),
  })
  return r.item as FeedbackItem
}

export async function myFeedback(): Promise<FeedbackItem[]> {
  const r = await api('/feedback/mine')
  return (r.items || []) as FeedbackItem[]
}

export async function allFeedback(): Promise<FeedbackItem[]> {
  const r = await api('/feedback')
  return (r.items || []) as FeedbackItem[]
}

/** 管理员更新处理进度（status + 可选处理说明）；提交人会看到同样的状态。 */
export async function updateFeedback(
  fid: string, status: FeedbackStatus, note: string,
): Promise<FeedbackItem> {
  const r = await api(`/feedback/${encodeURIComponent(fid)}`, {
    method: 'PATCH',
    body: JSON.stringify({ status, note }),
  })
  return r.item as FeedbackItem
}

/**
 * 撤回自己提交的反馈（连服务端的图片文件一起删）。
 * 只对「待处理」的单子有用 —— 管理员一动手（处理中/已处理）服务端会 400。
 */
export async function withdrawFeedback(fid: string): Promise<void> {
  await api(`/feedback/${encodeURIComponent(fid)}`, { method: 'DELETE' })
}
export async function archiveFeedback(fid: string, archived: boolean): Promise<FeedbackItem> {
  const r = await api(`/feedback/${encodeURIComponent(fid)}/archive`, {
    method: 'PATCH',
    body: JSON.stringify({ archived }),
  })
  return r.item as FeedbackItem
}
export async function adminDeleteFeedback(fid: string): Promise<void> {
  await api(`/feedback/${encodeURIComponent(fid)}`, { method: 'DELETE' })
}

/** 取某条反馈第 idx 张原图（返回 data URI；权限由服务端判：本人或 admin）。 */
export async function feedbackImage(fid: string, idx: number): Promise<string> {
  const r = await api(`/feedback/media/${encodeURIComponent(fid)}/${idx}`)
  return String(r.uri || '')
}
