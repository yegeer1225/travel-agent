import { useNavigate, useLocation, Link } from 'react-router-dom'
import AuthForm from '../components/AuthForm'

/**
 * 登录 / 注册（M9 鉴权前置页，15.4：用户已定不动视觉，只做表单复用改造）。
 * - 表单本体在 components/AuthForm.tsx（整页与登录弹窗共用，避免抄两份）
 * - onSuccess：存 token 已由 AuthForm 内部完成，这里只负责回跳 redirect 或 /profile
 */
export default function Login() {
  const nav = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from
  const redirect = from ?? new URLSearchParams(location.search).get('redirect') ?? '/profile'
  const initialMode = new URLSearchParams(location.search).get('mode') === 'register' ? 'register' : 'login'

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
          <AuthForm onSuccess={() => nav(redirect, { replace: true })} initialMode={initialMode} />
        </div>

        <div className="text-center mt-6 text-[12px] text-muted">
          <Link to="/" className="text-ink font-bold underline-offset-2 hover:underline">← 返回首页</Link>
        </div>
      </div>
    </div>
  )
}
