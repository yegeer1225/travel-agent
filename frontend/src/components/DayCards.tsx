import type { Day, Stop } from '../types/contract'
import CheckBadge from './CheckBadge'

function fmtTime(t: string | null): string {
  return t ?? '--:--'
}

/** 站点行：站级 checks 角标挂在行后面（天级判据在卡片头部，不要混） */
function StopRow({ stop }: { stop: Stop }) {
  return (
    <div className="flex items-center gap-3 py-2 border-t border-ink/15 first:border-t-0 text-[13px]">
      <span className="w-6 h-6 shrink-0 flex items-center justify-center bg-ink text-white font-display font-bold text-[12px] rounded-[2px]">
        {stop.seq}
      </span>
      <span className="font-medium truncate flex-1">{stop.name}</span>
      <span className="text-muted w-12 text-right shrink-0">{fmtTime(stop.arrive)}</span>
      <span className="text-muted w-14 text-right shrink-0">逗留 {stop.stay_min}分</span>
      <span className="text-muted w-20 text-right shrink-0">
        {stop.from_prev_km > 0 ? `${stop.from_prev_km} km` : '出发地'}
      </span>
      <span className="flex items-center gap-1 shrink-0">
        {stop.checks.map((c, i) => (
          <CheckBadge key={i} status={c.status} />
        ))}
      </span>
    </div>
  )
}

/** 按天分组卡片（静态展示，不含拖拽；拖拽编辑用 TripTable） */
export default function DayCards({ days }: { days: Day[] }) {
  return (
    <div className="flex flex-col gap-4">
      {days.map((day) => {
        const stats = day.day_stats
        return (
          <div key={day.day} className="card p-5">
            {/* 天级头部：日期 / 主题 / 天级 checks 与当日统计同一行 */}
            <div className="flex items-center gap-3 flex-wrap">
              <span className="font-display font-bold text-base shrink-0">DAY {day.day}</span>
              <span className="text-muted text-[13px] shrink-0">{day.date ?? '无日期'}</span>
              {day.theme && (
                <span className="text-[12px] px-2 py-0.5 border border-ink rounded-[2px] shrink-0">
                  {day.theme}
                </span>
              )}
              <div className="ml-auto flex items-center gap-4 text-[12px] text-muted shrink-0">
                {stats && (
                  <>
                    <span>当日 {stats.distance_km} km</span>
                    <span>车程 {stats.drive_min} 分钟</span>
                    <span>步行 {stats.walk_km} km</span>
                  </>
                )}
              </div>
            </div>

            {/* 天级判据（weather_conflict / walk_load / 当天车程）—— 渲染在当天卡片头部 */}
            {day.checks.length > 0 && (
              <div className="mt-2.5 flex items-start gap-2 flex-wrap">
                {day.checks.map((c, i) => (
                  <span key={i} className="flex items-center gap-1.5 text-[12px]">
                    <CheckBadge status={c.status} />
                    <span className="text-muted">{c.msg ?? c.code}</span>
                  </span>
                ))}
              </div>
            )}

            {/* 天气 */}
            {day.weather && day.weather.status === 'ok' && (
              <div className="mt-2 text-[12px] text-muted">
                白天 {day.weather.day_weather ?? '—'} {day.weather.day_temp != null ? `${day.weather.day_temp}°` : ''}
                {' · '}夜间 {day.weather.night_weather ?? '—'} {day.weather.night_temp != null ? `${day.weather.night_temp}°` : ''}
                {day.weather.note && ` · ${day.weather.note}`}
              </div>
            )}

            {/* 站点列表 */}
            <div className="mt-2">
              {day.stops.map((s) => (
                <StopRow key={s.seq} stop={s} />
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}
