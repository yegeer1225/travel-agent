import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'
import type { UserOut } from '../types/contract'
import { getMe } from '../lib/api'
import {
  clearToken,
  isLoggedIn,
  notifyLoginCancelled,
  notifyLoginSuccess,
  onAuthChange,
  setUnauthorizedHandler,
} from '../lib/auth'
import AuthModal from '../components/AuthModal'

const NAV_ITEMS = [
  { to: '/', label: '首页' },
  { to: '/spots', label: '旅游景点' },
  { to: '/assistant', label: 'AI路线规划助手' },
  { to: '/overview', label: '路线总览' },
  { to: '/guides', label: '旅游攻略' },
  { to: '/profile', label: '个人中心' },
]

export default function Layout() {
  const nav = useNavigate()

  // ── 登录态 / 弹窗（15.5：弹窗开/关由用户动作驱动，不在发请求时设置）──
  const [me, setMe] = useState<UserOut | null>(null)
  const [authOpen, setAuthOpen] = useState(false)
  const [authTitle, setAuthTitle] = useState('登录后继续')
  const [menuOpen, setMenuOpen] = useState(false)
  const pendingSearchRef = useRef<string | null>(null)

  const refreshMe = useCallback(() => {
    if (!isLoggedIn()) {
      setMe(null)
      return
    }
    getMe()
      .then((u) => setMe(u))
      .catch(() => setMe(null))
  }, [])

  useEffect(() => {
    refreshMe()
    // 登录 / 退出（setToken / clearToken）→ 刷新右侧头像昵称，页面不刷新
    const offAuth = onAuthChange(() => {
      refreshMe()
      setMenuOpen(false)
    })
    // 🔴 401 统一出口：开弹窗（当前页留在下层）。React 19 严格模式必须清理（同 6.3）
    setUnauthorizedHandler(() => {
      setAuthTitle('登录后继续')
      setAuthOpen(true)
    })
    return () => {
      offAuth()
      setUnauthorizedHandler(null)
    }
  }, [refreshMe])

  const closeAuth = useCallback(() => {
    setAuthOpen(false)
    notifyLoginCancelled() // 🔴 关闭必须通知，否则挂在 waitForLogin() 上的请求永远不返回
  }, [])

  const authSuccess = useCallback(() => {
    setAuthOpen(false) // 登录成功：关弹窗
    notifyLoginSuccess() // 解除 401 挂起 → 自动重放原 GET
    const kw = pendingSearchRef.current
    if (kw) {
      pendingSearchRef.current = null
      nav(`/spots?kw=${encodeURIComponent(kw)}`) // 未登录搜索：登录后带着关键词继续（15.3）
    }
  }, [nav])

  const onLogout = useCallback(() => {
    clearToken()
    setMe(null)
    setMenuOpen(false)
    nav('/') // SPA 跳转，不整页刷新
  }, [nav])

  // ── 导航栏搜索框（15.3：空词不做；未登录弹窗续搜；已登录跳 /spots?kw=）──
  const [searchKw, setSearchKw] = useState('')
  const submitSearch = (e: React.FormEvent) => {
    e.preventDefault()
    const kw = searchKw.trim()
    if (kw === '') return // 空搜索是误触：不跳页、不弹窗
    if (isLoggedIn()) {
      nav(`/spots?kw=${encodeURIComponent(kw)}`)
      return
    }
    pendingSearchRef.current = kw
    setAuthTitle('请先登录后搜索')
    setAuthOpen(true)
  }

  const navLinkCls = ({ isActive }: { isActive: boolean }) =>
    [
      'text-sm text-ink no-underline px-3.5 py-2 rounded-[4px] whitespace-nowrap transition-colors',
      isActive ? 'bg-pop-yellow font-bold' : 'hover:bg-pop-yellow/60',
    ].join(' ')

  return (
    <>
      <div className="page">
        {/* ── 导航栏 ── */}
        <header className="flex items-center gap-6 px-14 py-5 border-b border-ink bg-white relative z-10">
          <Link to="/" className="flex items-center gap-2.5 no-underline text-ink shrink-0">
            <div
              className="w-10 h-10 bg-ink text-white flex items-center justify-center rounded-[4px] font-display font-bold text-lg"
              style={{ transform: 'rotate(-2deg)' }}
            >
              TR
            </div>
            <div className="font-display font-bold text-[17px] tracking-wide">旅游规划平台</div>
          </Link>

          <nav className="flex gap-1.5 ml-auto items-center">
            {NAV_ITEMS.slice(0, 5).map((item) => (
              <NavLink key={item.to} to={item.to} end={item.to === '/'} className={navLinkCls}>
                {item.label}
              </NavLink>
            ))}

            {/* 搜索框：插在「旅游攻略」和「个人中心」中间（15.2，用户明确要求） */}
            <form onSubmit={submitSearch} className="flex items-center border border-ink rounded-[4px] overflow-hidden ml-1">
              <input
                className="w-[132px] px-2.5 py-[7px] text-[12px] outline-none border-0"
                placeholder="搜索景点…"
                value={searchKw}
                onChange={(e) => setSearchKw(e.target.value)}
                aria-label="搜索景点"
              />
              <button type="submit" className="px-2.5 text-[13px] hover:bg-pop-yellow/50 cursor-pointer border-l border-ink" aria-label="搜索">
                🔍
              </button>
            </form>

            {NAV_ITEMS.slice(5).map((item) => (
              <NavLink key={item.to} to={item.to} className={navLinkCls}>
                {item.label}
              </NavLink>
            ))}
          </nav>

          {/* 右侧：未登录 [注册/登录]；已登录 [头像+昵称 ▾]（二选一，不并列） */}
          <div className="w-[132px] shrink-0 flex justify-end">
            {me ? (
              <div className="relative">
                <button
                  className="flex items-center gap-2 text-[13px] cursor-pointer border border-ink rounded-[4px] px-2.5 py-1.5 bg-white hover:bg-pop-yellow/40"
                  onClick={() => setMenuOpen((v) => !v)}
                >
                  {me.avatar ? (
                    <img src={me.avatar} alt="" className="w-6 h-6 rounded-[4px] object-cover border border-ink" onError={(e) => { e.currentTarget.style.display = 'none' }} />
                  ) : (
                    <span className="w-6 h-6 bg-ink text-white flex items-center justify-center rounded-[4px] text-[11px] font-bold">
                      {(me.nickname ?? me.username ?? '?').slice(0, 1)}
                    </span>
                  )}
                  <span className="max-w-[70px] truncate">{me.nickname ?? me.username}</span>
                  <span className="text-[10px] text-muted">▾</span>
                </button>
                {menuOpen && (
                  <>
                    {/* 点击外部关闭 */}
                    <div className="fixed inset-0 z-40" onClick={() => setMenuOpen(false)} />
                    <div className="absolute right-0 mt-1.5 w-40 bg-white border border-ink rounded-[4px] z-50 py-1.5 shadow-none">
                      <button
                        className="w-full text-left px-4 py-2 text-[13px] hover:bg-pop-yellow/50 cursor-pointer"
                        onClick={() => { setMenuOpen(false); nav('/profile') }}
                      >
                        个人中心
                      </button>
                      <button
                        className="w-full text-left px-4 py-2 text-[13px] text-status-fail hover:bg-pop-yellow/50 cursor-pointer border-t border-ink/10"
                        onClick={onLogout}
                      >
                        退出登录
                      </button>
                    </div>
                  </>
                )}
              </div>
            ) : (
              <div className="flex gap-2">
                <Link to="/login" className="btn-outline !text-[12px] !px-3.5 !py-1.5">登录</Link>
                <Link to="/login?mode=register" className="btn-black !text-[12px] !px-3.5 !py-1.5">注册</Link>
              </div>
            )}
          </div>
        </header>

        {/* ── 页面主体：flex 链式撑满，子页面可用 flex-1 min-h-0 做内部滚动 ── */}
        <main className="flex-1 relative flex flex-col min-h-0">
          <Outlet />
        </main>

        {/* ── 页脚 ── */}
        <footer className="border-t border-ink px-14 py-7 flex justify-between text-[13px] text-muted relative z-10 bg-white">
          <span>TR 旅游规划平台</span>
          <span>1440px 桌面端 · 高德数据经后端代理</span>
        </footer>
      </div>

      {/* 全局登录弹窗（401 兜底 / 未登录搜索） */}
      <AuthModal open={authOpen} title={authTitle} onClose={closeAuth} onSuccess={authSuccess} />

      {/* 窄屏提示：只做桌面端 */}
      <div className="mobile-only-note fixed inset-0 items-center justify-center bg-white z-50">
        <div className="text-center px-8">
          <div className="font-display font-bold text-2xl mb-3">请在电脑上打开</div>
          <p className="text-muted text-sm leading-6">
            本产品为 1440px 桌面端设计，暂不支持手机访问。
          </p>
        </div>
      </div>
    </>
  )
}
