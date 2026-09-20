import { useEffect, useRef } from 'react'

/**
 * 全站第一个弹窗（内部设计规范 2.7）：
 * - 直角 4px + 白底 + 1px #E5E5E5 细边；遮罩 rgba(0,0,0,.35)，不加 blur
 * - 确认 = btn-black（黑实心 -1°），取消 = btn-outline；取消在左、确认在右
 * - Esc 取消；打开时焦点落在确认按钮；关闭后焦点还给触发它的按钮
 * - busy=true → 两个按钮都禁用（半途关闭会留下"不知道成没成"的孤儿状态）
 */
export default function ConfirmDialog({
  open,
  title,
  children,
  confirmText = '确认',
  cancelText = '取消',
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean
  title: string
  children: React.ReactNode
  confirmText?: string
  cancelText?: string
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const confirmRef = useRef<HTMLButtonElement>(null)
  const triggerRef = useRef<HTMLElement | null>(null)

  // 打开时记录触发按钮并聚焦确认按钮；关闭时归还焦点
  useEffect(() => {
    if (open) {
      triggerRef.current = document.activeElement as HTMLElement | null
      confirmRef.current?.focus()
    } else if (triggerRef.current) {
      triggerRef.current.focus?.()
      triggerRef.current = null
    }
  }, [open])

  // Esc 取消（busy 时不响应）
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) {
        e.stopPropagation()
        onCancel()
      }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [open, busy, onCancel])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: 'rgba(0,0,0,.35)' }}
      onMouseDown={(e) => {
        // 点遮罩 = 取消（busy 时忽略）
        if (e.target === e.currentTarget && !busy) onCancel()
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="w-[440px] max-w-[90vw] bg-white rounded-[4px] border"
        style={{ borderColor: '#E5E5E5' }}
      >
        <div className="px-6 pt-5 pb-4">
          <h3 className="font-display font-bold text-[18px]">{title}</h3>
        </div>
        <div className="px-6 pb-2 text-[14px] leading-6 text-ink">{children}</div>
        <div className="flex justify-end gap-3 px-6 py-5">
          <button className="btn-outline !text-[13px] !px-5 !py-2" onClick={onCancel} disabled={busy}>
            {cancelText}
          </button>
          <button
            ref={confirmRef}
            className="btn-black !text-[13px] !px-5 !py-2"
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? '处理中…' : confirmText}
          </button>
        </div>
      </div>
    </div>
  )
}
