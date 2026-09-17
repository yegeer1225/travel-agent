/**
 * 鉴权基础：token 存取 + 401 全局兜底（M9 已上线，真实 JWT）。
 *
 * 约定（api.md 1.1 / 1.2 / 1.1.3）：
 * - token 只存 localStorage，JWT 里只有 user_id + exp，前端不许解 token 取昵称/头像（走 GET /auth/me）
 * - 401 unauthorized → 清 token → 开全局登录弹窗（**不跳页、不清滚动**，15.5）
 * - GET 401 会挂在 waitForLogin() 上，登录成功后自动重放一次（只重放 GET，POST 不重放）
 * - 弹窗开/关由用户动作驱动：setUnauthorizedHandler 注册（App 挂载时），
 *   React 19 严格模式 useEffect 跑两次会重复注册 → 卸载必须传 null 清理
 */

const TOKEN_KEY = 'token'
const AUTH_EVENT = 'auth-change'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
  window.dispatchEvent(new Event(AUTH_EVENT))
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
  window.dispatchEvent(new Event(AUTH_EVENT))
}

export function isLoggedIn(): boolean {
  return getToken() !== ''
}

/** 登录态变化事件：Layout 监听它刷新右侧头像/昵称（退出登录页面不刷新） */
export function onAuthChange(fn: () => void): () => void {
  window.addEventListener(AUTH_EVENT, fn)
  return () => window.removeEventListener(AUTH_EVENT, fn)
}

// ── 401 → 弹窗（api.md 1.1.3 / 交接文档 15.9）──────────────────

type UnauthHandler = () => void
let onUnauth: UnauthHandler | null = null
let loginPromise: Promise<void> | null = null
let resolveLogin: (() => void) | null = null
let rejectLogin: ((e: unknown) => void) | null = null

/** App/Layout 挂载时注册（卸载传 null）。弹窗由它开 */
export function setUnauthorizedHandler(fn: UnauthHandler | null): void {
  onUnauth = fn
}

/** 401 统一出口：清 token + 开弹窗。**不跳转、不碰 location、不清滚动** */
export function handleUnauthorized(): void {
  clearToken()
  onUnauth?.()
}

/**
 * 等这次登录有结果。
 * 🔴 并发 401 必须共享同一个 Promise —— 否则 N 个请求 = N 个弹窗，
 *    而且只有最后一个的重放能成功（前面的 resolve 被覆盖掉了）。
 */
export function waitForLogin(): Promise<void> {
  if (!loginPromise) {
    loginPromise = new Promise<void>((res, rej) => {
      resolveLogin = res
      rejectLogin = rej
    })
  }
  return loginPromise
}

export function notifyLoginSuccess(): void {
  resolveLogin?.()
  loginPromise = resolveLogin = rejectLogin = null
}

export function notifyLoginCancelled(): void {
  rejectLogin?.(new Error('login_cancelled'))
  loginPromise = resolveLogin = rejectLogin = null
}
