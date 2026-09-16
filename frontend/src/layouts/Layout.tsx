import { Link, NavLink, Outlet } from 'react-router-dom'

const NAV_ITEMS = [
  { to: '/', label: '首页' },
  { to: '/spots', label: '旅游景点' },
  { to: '/assistant', label: 'AI路线规划助手' },
  { to: '/overview', label: '路线总览' },
  { to: '/guides', label: '旅游攻略' },
  { to: '/profile', label: '个人中心' },
]

export default function Layout() {
  return (
    <>
      <div className="page">
        {/* ── 导航栏 ── */}
        <header
          className="flex items-center gap-8 px-14 py-5 border-b border-ink bg-white relative z-10"
        >
          <Link to="/" className="flex items-center gap-2.5 no-underline text-ink">
            <div
              className="w-10 h-10 bg-ink text-white flex items-center justify-center rounded-[4px] font-display font-bold text-lg"
              style={{ transform: 'rotate(-2deg)' }}
            >
              TR
            </div>
            <div className="font-display font-bold text-[17px] tracking-wide">旅游规划平台</div>
          </Link>

          <nav className="flex gap-1.5 ml-auto">
            {NAV_ITEMS.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === '/'}
                className={({ isActive }) =>
                  [
                    'text-sm text-ink no-underline px-3.5 py-2 rounded-[4px] whitespace-nowrap transition-colors',
                    isActive ? 'bg-pop-yellow font-bold' : 'hover:bg-pop-yellow/60',
                  ].join(' ')
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
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
