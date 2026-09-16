import type { CheckStatus } from '../types/contract'

const STATUS_META: Record<CheckStatus, { label: string; mark: string; cls: string }> = {
  pass: { label: '通过', mark: '✅', cls: 'status-pass' },
  fail: { label: '不通过', mark: '⛔', cls: 'status-fail' },
  unknown: { label: '无法判定', mark: '⚪', cls: 'status-unknown' },
}

/** 三态通用胶囊：pass / fail / unknown，颜色一律用 --color-status-*（语义色，不碰装饰色） */
export default function StatusPill({ status, label }: { status: CheckStatus; label?: string }) {
  const meta = STATUS_META[status]
  return (
    <span className={`status-pill ${meta.cls}`}>
      <span>{meta.mark}</span>
      {label ?? meta.label}
    </span>
  )
}
