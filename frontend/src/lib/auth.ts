/**
 * 鉴权基础：token 存取 + 401 全局兜底（M9 已上线，真实 JWT）。
 *
 * 约定（api.md 1.1 / 1.2）：
 * - token 只存 localStorage，JWT 里只有 user_id + exp，前端不许解 token 取昵称/头像（走 GET /auth/me）
 * - 401 unauthorized → 清 token → 跳登录页（带 redirect 回跳）
 * - /api/health、/api/auth/register、/api/auth/login 不要求 token，其余全部要求
 */

const TOKEN_KEY = 'token'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

export function isLoggedIn(): boolean {
  return getToken() !== ''
}

/** 401 统一出口：清 token + 跳登录（带 redirect，登录成功后回跳） */
export function handleUnauthorized(): void {
  clearToken()
  const cur = window.location.pathname + window.location.search
  if (!cur.startsWith('/login')) {
    window.location.assign('/login?redirect=' + encodeURIComponent(cur))
  }
}
