import type { Day } from '../types/contract'

/** Day 页签：切页签只显示当天路线（TripMap 需要 destroy 再重建） */
export default function DayTabs({
  days,
  active,
  onChange,
}: {
  days: Day[]
  active: number | null
  onChange: (day: number) => void
}) {
  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <span className="text-[12px] text-muted mr-1 shrink-0">按天查看</span>
      {days.map((d) => (
        <button
          key={d.day}
          onClick={() => onChange(d.day)}
          className={[
            'px-3.5 py-1.5 text-[13px] font-bold border border-ink rounded-[4px] transition-colors cursor-pointer',
            active === d.day ? 'bg-pop-yellow text-ink' : 'bg-white text-ink hover:bg-pop-yellow/50',
          ].join(' ')}
        >
          Day {d.day}
          {d.date && <span className="ml-1 font-normal text-[11px] text-muted">{d.date.slice(5)}</span>}
        </button>
      ))}
    </div>
  )
}
