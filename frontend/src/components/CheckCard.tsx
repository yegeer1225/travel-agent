import type { Check, CheckStatus } from '../types/contract'

const STATUS_META: Record<CheckStatus, { mark: string; label: string; cls: string }> = {
  pass: { mark: '✅', label: '通过', cls: 'text-status-pass' },
  fail: { mark: '⛔', label: '不通过', cls: 'text-status-fail' },
  unknown: { mark: '⚪', label: '无法判定', cls: 'text-status-unknown' },
}

function groupByStatus(checks: Check[]): Record<CheckStatus, Check[]> {
  const groups: Record<CheckStatus, Check[]> = { pass: [], fail: [], unknown: [] }
  for (const c of checks) groups[c.status].push(c)
  return groups
}

/**
 * 校验卡（SSE `check` 事件 / recheck 响应 / Trip.checks 共用）。
 * - fail：level=hard 显示"不通过"；level=soft 显示"有风险"（不打回重排）
 * - unknown：必须显示 msg 里的原因，绝不显示成"通过"的样子
 * - 判据说明一律读 msg（后端给好了人话），前端不做文案分支
 */
export default function CheckCard({
  checks,
  round,
}: {
  checks: Check[]
  round?: number
}) {
  const groups = groupByStatus(checks)
  const counts: Record<CheckStatus, number> = {
    pass: groups.pass.length,
    fail: groups.fail.length,
    unknown: groups.unknown.length,
  }

  return (
    <div className="card p-4">
      <div className="flex items-center gap-3 mb-3">
        <span className="font-display font-bold text-sm tracking-wide uppercase">校验</span>
        {round !== undefined && (
          <span className="text-xs text-muted border border-ink rounded-[2px] px-2 py-0.5">
            round {round}
          </span>
        )}
        <div className="ml-auto flex items-center gap-3 text-xs">
          {(['pass', 'fail', 'unknown'] as CheckStatus[]).map((s) => (
            <span key={s} className={`flex items-center gap-1 ${STATUS_META[s].cls}`}>
              {STATUS_META[s].mark} {counts[s]}
            </span>
          ))}
        </div>
      </div>

      {checks.length === 0 && <div className="text-xs text-muted">暂无校验判据</div>}

      {(['fail', 'unknown'] as CheckStatus[]).map((s) =>
        groups[s].length === 0 ? null : (
          <div key={s} className="mb-2 last:mb-0">
            {groups[s].map((c, i) => (
              <div key={i} className="flex items-start gap-2 py-1.5 border-t border-ink/15 text-[13px] leading-5">
                <span className={STATUS_META[s].cls}>{STATUS_META[s].mark}</span>
                <span className={STATUS_META[s].cls}>
                  {s === 'fail' ? (c.level === 'soft' ? '有风险' : '不通过') : '无法判定'}
                </span>
                <span className="text-muted flex-1">
                  {c.msg ?? c.code}
                  {c.msg && c.code !== c.msg && (
                    <span className="text-[11px] text-muted/70"> · {c.code}</span>
                  )}
                </span>
              </div>
            ))}
          </div>
        ),
      )}

      {groups.pass.length > 0 && (
        <div className="text-[12px] text-muted border-t border-ink/15 pt-1.5 mt-1">
          {groups.pass.length} 项判据通过
        </div>
      )}
    </div>
  )
}
