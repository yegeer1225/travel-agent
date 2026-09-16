import type { Trip } from '../types/contract'

/**
 * 行程卡（SSE `trip` 事件渲染）。
 * - 总里程口径：summary.total_distance_km = 站间里程之和，标签就叫「总里程」，不加副标
 * - source=generated 且 session_id 非空 → 可回助手页接着聊 → 「生成路线总览」
 */
export default function TripCard({
  trip,
  onOpenOverview,
}: {
  trip: Trip
  onOpenOverview?: (tripId: string) => void
}) {
  const dayCount = trip.days.length
  const stopCount = trip.summary.stop_count
  const canContinue = trip.source === 'generated' && trip.session_id !== null

  return (
    <div className="card p-5 raise">
      <div className="flex items-start gap-4">
        <div
          className="w-12 h-12 shrink-0 bg-pop-yellow border border-ink rounded-[4px] flex items-center justify-center font-display font-bold text-lg"
          style={{ transform: 'rotate(-2deg)' }}
        >
          {trip.days.length}D
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="font-bold text-[17px] truncate">{trip.title}</h3>
            {canContinue && (
              <span className="text-[11px] px-2 py-0.5 border border-ink rounded-[2px] text-muted shrink-0">
                可继续对话
              </span>
            )}
          </div>
          <div className="text-[13px] text-muted mt-0.5">
            {trip.destination} · {dayCount} 天 · {stopCount} 站
          </div>
        </div>
        <div className="text-right shrink-0">
          <div className="font-display font-bold text-xl">{(trip.summary.total_distance_km ?? 0).toFixed(1)} km</div>
          <div className="text-[11px] text-muted">总里程</div>
        </div>
      </div>

      <div className="flex items-center gap-4 mt-3 pt-3 border-t border-ink/15 text-[12px]">
        <span className="text-status-pass">✅ 硬错 {trip.summary.hard_errors}</span>
        <span className="text-status-unknown">⚠️ 软警告 {trip.summary.soft_warnings}</span>
        {trip.checks.length > 0 && <span className="text-muted">判据 {trip.checks.length} 条</span>}
        <div className="ml-auto flex gap-2">
          {onOpenOverview && (
            <button className="btn-black !text-[12px] !px-4 !py-2" onClick={() => onOpenOverview(trip.trip_id)}>
              生成路线总览
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
