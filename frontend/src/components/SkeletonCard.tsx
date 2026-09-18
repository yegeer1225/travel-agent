import type { SkeletonEvent } from '../types/contract'

/**
 * 骨架行程卡（18 节 skeleton 事件）—— 只读、快速草排：
 * - stops 是「未验证地名文本」，只能渲染成纯文本（→ 连接），不可点击、不进详情、不显示 id
 * - note 是骨架与正式行程的区别声明，必须原文显示，不能吞
 */
export default function SkeletonCard({ evt }: { evt: SkeletonEvent }) {
  return (
    <div className="card p-4 border-ink/60 bg-pop-yellow/20">
      <div className="flex items-center gap-2">
        <span className="font-display font-bold text-[15px]">{evt.title}</span>
        <span className="text-[10px] px-1.5 py-0.5 border border-ink/40 rounded-[2px] text-muted shrink-0">
          草排
        </span>
      </div>
      <div className="mt-2 flex flex-col gap-1.5">
        {evt.days.map((d) => (
          <div key={d.day} className="text-[13px] leading-5">
            <span className="font-bold">Day {d.day}</span>
            {d.date && <span className="text-muted"> · {d.date}</span>}
            <span className="text-muted"> · {d.theme}</span>
            <span className="text-muted"> · {d.stops.join(' → ')}</span>
          </div>
        ))}
      </div>
      <p className="mt-2 pt-2 border-t border-ink/15 text-[12px] text-muted leading-5">{evt.note}</p>
    </div>
  )
}
