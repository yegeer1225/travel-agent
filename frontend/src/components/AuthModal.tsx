import { useEffect, useRef } from 'react'
import AuthForm from './AuthForm'
import { notifyLoginSuccess } from '../lib/auth'

/**
 * 全局登录弹窗（api.md 1.1.3 / 交接文档 15.5）：
 * - 401 触发：Layout 注册的 setUnauthorizedHandler 打开它，当前页留在下层（不卸载、不清滚动）
 * - 登录成功 → notifyLoginSuccess() → 挂在 waitForLogin() 上的 GET 自动重放
 * - 关闭（Esc / 遮罩 / ×）→ notifyLoginCancelled() → 挂起请求 reject（不然永远不返回）
 * - 🔴 只弹一个：authOpen 是全局唯一 state，连点多个需要登录的按钮也只弹一次
 */
export default function AuthModal({
  open,
  title = '登录后继续',
  onClose,
  onSuccess,
}: {
  open: boolean
  title?: string
  onClose: () => void
  /** 登录成功回调（默认 notifyLoginSuccess 解除 401 挂起；Layout 可追加"未登录搜索"续跳逻辑） */
  onSuccess?: () => void
}) {
  const cardRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    // 焦点进弹窗
    cardRef.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [open, onClose])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: 'rgba(0,0,0,.35)' }}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={cardRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="w-[400px] max-w-[90vw] bg-white rounded-[4px] border outline-none px-6 py-6"
        style={{ borderColor: '#E5E5E5' }}
      >
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-display font-bold text-[18px]">{title}</h3>
          <button
            className="text-muted hover:text-ink text-[18px] leading-none px-1 cursor-pointer"
            onClick={onClose}
            aria-label="关闭"
          >
            ×
          </button>
        </div>

        <AuthForm compact onSuccess={onSuccess ?? notifyLoginSuccess} />

        <p className="text-[11px] text-muted mt-4 leading-5">
          登录后，刚才的操作会自动继续（浏览类操作）。收藏、评论等写操作登录后需再点一次。
        </p>
      </div>
    </div>
  )
}
