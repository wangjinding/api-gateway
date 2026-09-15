// 设置页工具层 —— 对话默认参数，统一读写 localStorage。
// 注：主题（A/B/C + 跟随系统）已独立到 lib/theme.ts；这里只管参数。

export interface ChatDefaults {
  model?: string
  temperature?: number
  top_p?: number
  max_tokens?: number | null
  stream?: boolean
  system?: string
}

const DEFAULTS_KEY = 'gw_chat_defaults'

export function getChatDefaults(): ChatDefaults {
  try {
    const raw = localStorage.getItem(DEFAULTS_KEY)
    return raw ? (JSON.parse(raw) as ChatDefaults) : {}
  } catch {
    return {}
  }
}

export function setChatDefaults(d: ChatDefaults): void {
  localStorage.setItem(DEFAULTS_KEY, JSON.stringify(d))
}
