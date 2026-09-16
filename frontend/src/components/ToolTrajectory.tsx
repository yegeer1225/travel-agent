import type { NodeEvent, ToolCallEvent, ToolResultEvent } from '../types/contract'

/** 轨迹条目（页面内部状态，由 upsertTrajectory 维护） */
export interface TrajectoryEntry {
  key: string
  kind: 'node' | 'tool'
  node?: string
  tool?: string
  label: string
  done: boolean
  elapsed_ms: number | null
  ok?: boolean
  summary?: string
  degraded?: boolean
}

type TrajEvent = NodeEvent | ToolCallEvent | ToolResultEvent

/** 把 SSE 的 node / tool_call / tool_result 事件 upsert 进轨迹列表（不可变更新） */
export function upsertTrajectory(
  prev: TrajectoryEntry[],
  evt: TrajEvent,
  counters: Record<string, number> = {},
): TrajectoryEntry[] {
  if (evt.type === 'node') {
    if (evt.phase === 'start') {
      const n = (counters[evt.node] ?? 0) + 1
      counters[evt.node] = n
      const key = `node:${evt.node}#${n}`
      return [
        ...prev,
        { key, kind: 'node', node: evt.node, label: evt.label, done: false, elapsed_ms: null },
      ]
    }
    // phase === 'end'：把该 node 最近一条未完成实例标记完成
    const idx = prev.findIndex((e) => e.kind === 'node' && e.node === evt.node && !e.done)
    if (idx < 0) return prev
    const next = prev.slice()
    next[idx] = { ...next[idx], done: true, elapsed_ms: evt.elapsed_ms }
    return next
  }

  if (evt.type === 'tool_call') {
    return [
      ...prev,
      {
        key: `tool:${evt.call_id}`,
        kind: 'tool',
        tool: evt.tool,
        label: evt.label,
        done: false,
        elapsed_ms: null,
      },
    ]
  }

  // tool_result
  const idx = prev.findIndex((e) => e.key === `tool:${evt.call_id}`)
  if (idx < 0) return prev
  const next = prev.slice()
  next[idx] = {
    ...next[idx],
    done: true,
    ok: evt.ok,
    summary: evt.summary,
    degraded: evt.degraded,
  }
  return next
}

/** 轨迹卡：渲染 node / tool_call / tool_result 事件流（区别于打字机 token） */
export default function ToolTrajectory({ entries }: { entries: TrajectoryEntry[] }) {
  if (entries.length === 0) return null
  return (
    <div className="card p-4">
      <div className="font-display font-bold text-sm tracking-wide uppercase mb-3">执行轨迹</div>
      <ul className="flex flex-col gap-1.5">
        {entries.map((e) => (
          <li key={e.key} className="flex items-start gap-2.5 text-[13px] leading-5">
            <span className="mt-1.5 w-2 h-2 shrink-0 rounded-[2px] border border-ink bg-white">
              {e.done && (
                <span className="block w-2 h-2 bg-pop-green" style={{ margin: '-1px' }} />
              )}
            </span>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="font-bold text-ink">{e.label}</span>
                {e.done && e.elapsed_ms !== null && (
                  <span className="text-[11px] text-muted">{e.elapsed_ms}ms</span>
                )}
                {e.kind === 'tool' && e.done && e.degraded && (
                  <span className="text-[10px] px-1.5 border border-ink rounded-[2px] text-muted">
                    degraded
                  </span>
                )}
              </div>
              {e.kind === 'tool' && e.done && e.summary && (
                <div className="text-muted text-[12px] truncate">
                  {e.ok ? '✓ ' : '✗ '}
                  {e.summary}
                </div>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
