import { useEffect, useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import type { UserOut } from '../types/contract'
import { getMe } from '../lib/api'
import { clearToken } from '../lib/auth'

/**
 * 个人中心 · 鉴权闭环（M9）：
 * - 昵称/头像只从 GET /auth/me 拿（不许解 token）
 * - 退出登录 = 清 token 回登录页
 * - 我的行程 / 收藏列表在后续里程碑补充
 */
export default function Profile() {
  const nav = useNavigate()
  const [user, setUser] = useState<UserOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    getMe()
      .then((u) => alive && setUser(u))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [])

  const logout = () => {
    clearToken()
    nav('/login', { replace: true })
  }

  return (
    <div className="relative px-14 py-10">
      <div className="deco deco-ring" style={{ width: 26, height: 26, bottom: 26, left: 72 }} />

      <h1 className="font-display font-bold text-[34px]">个人中心</h1>
      <p className="text-[14px] text-muted mt-1">账号 · 我的行程 · 收藏</p>

      {/* 当前用户 */}
      <div className="card mt-8 p-6 max-w-[560px]">
        {loading && <div className="text-muted text-[13px]">加载中…</div>}
        {error && !loading && <div className="text-status-fail text-[13px]">加载失败：{error}</div>}
        {user && !loading && (
          <div className="flex items-center gap-5">
            {/* 头像：有 URL 显示图，没有就首字母色块（0 假数据） */}
            {user.avatar ? (
              <img
                src={user.avatar}
                alt="头像"
                className="w-16 h-16 rounded-[4px] border border-ink object-cover"
                onError={(e) => {
                  e.currentTarget.style.display = 'none'
                }}
              />
            ) : (
              <div className="w-16 h-16 rounded-[4px] border border-ink bg-pop-cyan flex items-center justify-center font-display font-bold text-xl">
                {(user.nickname ?? user.username).slice(0, 1).toUpperCase()}
              </div>
            )}
            <div className="min-w-0 flex-1">
              <div className="font-bold text-[17px]">{user.nickname ?? user.username}</div>
              <div className="text-[13px] text-muted mt-0.5">@{user.username}</div>
              {user.email && <div className="text-[12px] text-muted mt-0.5">{user.email}</div>}
            </div>
            <button className="btn-outline !text-[12px]" onClick={logout}>
              退出登录
            </button>
          </div>
        )}
      </div>

      <div className="card mt-6 p-8 text-center max-w-[560px]">
        <div className="font-display font-bold text-lg mb-2">更多功能建设中</div>
        <p className="text-muted text-[14px] leading-6 max-w-[480px] mx-auto">
          我的行程、收藏与昵称头像修改将在后续里程碑上线。
          <br />
          你可以先到 <Link to="/overview" className="text-ink font-bold underline-offset-4">路线总览</Link>{' '}
          查看已生成的行程。
        </p>
      </div>
    </div>
  )
}
