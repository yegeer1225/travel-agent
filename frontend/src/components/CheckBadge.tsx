import type { CheckStatus } from '../types/contract'

const MARK: Record<CheckStatus, string> = {
  pass: '✅',
  fail: '⛔',
  unknown: '⚪',
}

const COLOR: Record<CheckStatus, string> = {
  pass: 'text-status-pass',
  fail: 'text-status-fail',
  unknown: 'text-status-unknown',
}

/** 三态角标：只画符号，不写字。挂在站点行 / 天级卡片头部 */
export default function CheckBadge({ status }: { status: CheckStatus }) {
  return (
    <span className={`inline-flex items-center text-sm leading-none ${COLOR[status]}`} title={status}>
      {MARK[status]}
    </span>
  )
}
