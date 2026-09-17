import { useState } from 'react'
import { ApiError, register, login } from '../lib/api'
import { setToken } from '../lib/auth'

type Mode = 'login' | 'register'

/**
 * 登录 / 注册表单（M9 + 15.5）：整页（Login.tsx）和登录弹窗（AuthModal.tsx）渲染**同一个组件**。
 * - setToken 在这里做（两边都要存）；onSuccess 只管"接下来干什么"
 *   —— 整页是跳转，弹窗是 notifyLoginSuccess() 通知重放
 * - 错误按 code 显示（invalid_param / conflict / unauthorized）
 */
export default function AuthForm({
  onSuccess,
  compact = false,
  initialMode = 'login',
}: {
  onSuccess: () => void
  /** 弹窗模式：标题与间距收窄 */
  compact?: boolean
  /** 初始模式（导航栏「注册」→ /login?mode=register） */
  initialMode?: Mode
}) {
  const [mode, setMode] = useState<Mode>(initialMode)
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
      onSuccess()
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
    <div>
      {/* 模式切换 */}
      <div className="flex gap-1.5 mb-5">
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

      <h1 className={`font-display font-bold ${compact ? 'text-[20px]' : 'text-[26px]'} mb-1`}>
        {mode === 'login' ? '欢迎回来' : '创建账号'}
      </h1>
      <p className="text-[13px] text-muted mb-5">
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
  )
}
