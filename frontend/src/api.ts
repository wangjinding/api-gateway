// 与网关同源的请求层：所有调用带登录后换来的 sess- token。
// M5+: 登录改用账号密码（账号密码只在 /auth/login 提交一次，之后前端不持有明文口令）。

export const TOKEN_KEY = 'gw_token'
export const USER_KEY = 'gw_user'
export const ROLE_KEY = 'gw_role'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(token: string, user: string, role = ''): void {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, user || '')
  localStorage.setItem(ROLE_KEY, role || '')
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
  localStorage.removeItem(ROLE_KEY)
}

/** 从错误响应体里取出人能看懂的原因。

  网关自己抛的 HTTPException 一律是 OpenAI 形状 `{"error":{"message":"账号已被停用，请联系管理员"}}`，
  而 FastAPI 原生的才是 `{"detail": ...}`。以前各页面只读 `body.detail`，于是网关的报错全被
  吞成「HTTP 400 / HTTP 403」—— 这里统一认两种信封，页面拿 e.message 就能直接展示。
  结构化 detail（如路由失败时的 {message, errors[]}）取其中的 message。 */
function errorMessage(body: any): string {
  if (!body) return ''
  if (typeof body === 'string') return body
  const d = body.detail
  if (typeof d === 'string') return d
  if (d && typeof d === 'object') return String(d.message || '')
  const m = body.error && body.error.message
  return m ? String(m) : ''
}

/** 把捕获到的错误转成一句可展示的文案（页面 toast / 提示条直接用）。 */
export function errText(e: any): string {
  return errorMessage(e?.body) || (e && e.message) || String(e ?? '')
}

/** 账号密码登录 → 会话 token（登录唯一入口）。 */
export async function login(username: string, password: string): Promise<any> {
  const resp = await fetch('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  const body = await parse(resp)
  if (!resp.ok) {
    // 与 api() 同形：抛 Error（带 status / body），message 里放服务端的真实原因
    const err: any = new Error(errorMessage(body) || `HTTP ${resp.status}`)
    err.status = resp.status
    err.body = body
    throw err
  }
  return body
}

/** 通用网关调用（自动带会话 Bearer）。 */
export async function api(path: string, opts: any = {}): Promise<any> {
  const headers: Record<string, string> = Object.assign(
    { 'Content-Type': 'application/json' },
    opts.headers || {},
  )
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  const resp = await fetch(path, Object.assign({}, opts, { headers }))
  const body = await parse(resp)
  if (!resp.ok) {
    // 会话过期：清空本地 token 并广播，让界面回登录页重新登录（避免 401/429 死循环）
    if (resp.status === 401 && getToken()) {
      clearToken()
      window.dispatchEvent(new Event('gw-auth-expired'))
    }
    const err: any = new Error(errorMessage(body) || `HTTP ${resp.status}`)
    err.status = resp.status
    err.body = body
    throw err
  }
  return body
}

async function parse(resp: Response): Promise<any> {
  const text = await resp.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}