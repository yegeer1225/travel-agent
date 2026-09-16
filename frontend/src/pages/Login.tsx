import { useState } from 'react'
import { useNavigate, useLocation, Link } from 'react-router-dom'
import { ApiError, register, login } from '../lib/api'
import { setToken } from '../lib/auth'

type Mode = 'login' | 'register'

/**
 * 登录 / 注册（M9 鉴权前置页）。
 * - 登录成功拿 AuthResponse{token,...} → 存 localStorage → 回跳 redirect 或 /profile
 * - 错误按 code 显示（invalid_param 显示在表单下方、conflict 用户名已存在、unauthorized 用户名或密码错误）
 */
export default function Login() {
  const nav = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from
  const redirect = from ?? new URLSearchParams(location.search).get('redirect') ?? '/profile'

  const [mode, setMode] = useState<Mode>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [nickname, setNickname] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!username.trim() || !password) {
      setError('请输入用户名和密码')
      return
    }
    if (mode === 'register' && password.length < 6) {
      setError('密码至少 6 位')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const auth =
        mode === 'login'
          ? await login({ username: username.trim(), password })
          : await register({
              username: username.trim(),
              password,
              nickname: nickname.trim() === '' ? null : nickname.trim(),
            })
      setToken(auth.token)
      nav(redirect, { replace: true })
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.code === 'conflict'
            ? '用户名已存在，换个试试'
            : err.code === 'unauthorized'
              ? '用户名或密码错误'
              : err.message,
        )
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setLoading(false)
    }
  }

  const switchMode = (m: Mode) => {
    setMode(m)
    setError(null)
  }

  return (
    <div className="page items-center justify-center">
      {/* 装饰：登录页可适度，全部落留白处 */}
      <div className="deco deco-ring" style={{ width: 46, height: 46, top: 90, left: 130 }} />
      <div className="deco deco-tri" style={{ width: 42, height: 38, background: 'var(--color-pop-blue)', bottom: 120, left: 96, transform: 'rotate(18deg)' }} />
      <div className="deco deco-circle" style={{ width: 150, height: 150, background: 'var(--color-pop-red)', opacity: 0.85, top: 40, right: -46 }} />
      <div className="deco deco-half" style={{ width: 40, height: 80, background: 'var(--color-pop-green)', bottom: 70, right: 110 }} />
      <div className="deco deco-dots" style={{ width: 88, height: 48, top: 220, right: 180, opacity: 0.6 }} />

      <div className="relative z-[2] w-[460px]">
        {/* Logo */}
        <Link to="/" className="flex items-center gap-2.5 no-underline text-ink mb-8 justify-center">
          <div className="w-10 h-10 bg-ink text-white flex items-center justify-center rounded-[4px] font-display font-bold text-lg" style={{ transform: 'rotate(-2deg)' }}>
            TR
          </div>
          <div className="font-display font-bold text-[17px] tracking-wide">旅游规划平台</div>
        </Link>

        <div className="card p-8 raise raise-yellow">
          {/* 模式切换 */}
          <div className="flex gap-1.5 mb-6">
            {(['login', 'register'] as Mode[]).map((m) => (
              <button
                key={m}
                onClick={() => switchMode(m)}
                className={[
                  'flex-1 py-2 text-[14px] font-bold border border-ink rounded-[4px] transition-colors cursor-pointer',
                  mode === m ? 'bg-pop-yellow' : 'bg-white hover:bg-pop-yellow/40',
                ].join(' ')}
              >
                {m === 'login' ? '登录' : '注册'}
              </button>
            ))}
          </div>

          <h1 className="font-display font-bold text-[26px] mb-1">
            {mode === 'login' ? '欢迎回来' : '创建账号'}
          </h1>
          <p className="text-[13px] text-muted mb-6">
            {mode === 'login' ? '登录后继续你的行程规划' : '注册后立即开始规划行程'}
          </p>

          <form onSubmit={submit} className="flex flex-col gap-4">
            <label className="flex flex-col gap-1.5 text-[13px]">
              <span className="font-bold">用户名</span>
              <input
                className="border border-ink rounded-[4px] px-3 py-2.5 outline-none focus:border-2"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="username"
                autoComplete="username"
              />
            </label>

            {mode === 'register' && (
              <label className="flex flex-col gap-1.5 text-[13px]">
                <span className="font-bold">昵称（可选）</span>
                <input
                  className="border border-ink rounded-[4px] px-3 py-2.5 outline-none focus:border-2"
                  value={nickname}
                  onChange={(e) => setNickname(e.target.value)}
                  placeholder="展示给其他人看的名字"
                />
              </label>
            )}

            <label className="flex flex-col gap-1.5 text-[13px]">
              <span className="font-bold">密码</span>
              <input
                type="password"
                className="border border-ink rounded-[4px] px-3 py-2.5 outline-none focus:border-2"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={mode === 'register' ? '至少 6 位' : '••••••••'}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              />
            </label>

            {error && <div className="text-status-fail text-[13px]">{error}</div>}

            <button type="submit" className="btn-black w-full" disabled={loading}>
              {loading ? (mode === 'login' ? '登录中…' : '注册中…') : mode === 'login' ? '登录' : '注册'}
            </button>
          </form>

          <div className="text-[12px] text-muted mt-4">
            {mode === 'login' ? (
              <>还没有账号？<button className="text-ink font-bold underline-offset-2 hover:underline cursor-pointer" onClick={() => switchMode('register')}>去注册</button></>
            ) : (
              <>已有账号？<button className="text-ink font-bold underline-offset-2 hover:underline cursor-pointer" onClick={() => switchMode('login')}>去登录</button></>
            )}
          </div>
        </div>

        <div className="text-center mt-6 text-[12px] text-muted">
          <Link to="/" className="text-ink font-bold underline-offset-2 hover:underline">← 返回首页</Link>
        </div>
      </div>
    </div>
  )
}
