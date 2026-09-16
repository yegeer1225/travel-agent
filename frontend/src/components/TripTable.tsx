import { useState } from 'react'
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  useSensor,
  useSensors,
  useDraggable,
  useDroppable,
  type DragEndEvent,
  type DragStartEvent,
} from '@dnd-kit/core'
import type { Stop, Trip, TripOp } from '../types/contract'
import CheckBadge from './CheckBadge'

/* ── 拖拽行（同时是可拖拽源 + 可插入目标）────────────────── */
function DraggableStopRow({
  stop,
  day,
  dragging,
  onDelete,
  onCommitTime,
}: {
  stop: Stop
  day: number
  dragging: boolean
  onDelete: (day: number, seq: number) => void
  onCommitTime: (day: number, seq: number, arrive: string | null, stayMin: number | null) => void
}) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: `stop-${day}-${stop.seq}`,
    data: { day, seq: stop.seq, isStop: true, name: stop.name },
  })
  const { setNodeRef: setDropRef, isOver } = useDroppable({
    id: `drop-stop-${day}-${stop.seq}`,
    data: { day, seq: stop.seq, isStopDrop: true },
  })

  const [editing, setEditing] = useState<'arrive' | 'stay' | null>(null)
  const [arrive, setArrive] = useState(stop.arrive ?? '')
  const [stay, setStay] = useState(String(stop.stay_min))

  const commit = () => {
    const a = arrive.trim() === '' ? null : arrive.trim()
    const s = stay.trim() === '' ? null : Number(stay.trim())
    onCommitTime(day, stop.seq, a, s)
    setEditing(null)
  }

  return (
    <div
      ref={(node) => {
        setNodeRef(node)
        setDropRef(node)
      }}
      className={[
        'flex items-center gap-3 py-2 border-t border-ink/15 first:border-t-0 text-[13px] group',
        isDragging ? 'opacity-30' : '',
        isOver && !dragging ? 'outline outline-2 outline-status-pass -outline-offset-2' : '',
      ].join(' ')}
    >
      {/* 拖拽手柄 */}
      <button
        className="w-5 h-6 shrink-0 flex items-center justify-center text-muted cursor-grab active:cursor-grabbing hover:text-ink select-none"
        {...attributes}
        {...listeners}
        aria-label="拖拽排序"
      >
        ⠿
      </button>

      <span className="w-6 h-6 shrink-0 flex items-center justify-center bg-ink text-white font-display font-bold text-[12px] rounded-[2px]">
        {stop.seq}
      </span>
      <span className="font-medium truncate flex-1">{stop.name}</span>

      {/* 到达时间（可点编辑） */}
      {editing === 'arrive' ? (
        <input
          className="w-14 border border-ink rounded-[2px] px-1 text-[12px] outline-none"
          value={arrive}
          onChange={(e) => setArrive(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === 'Enter' && commit()}
          autoFocus
        />
      ) : (
        <button
          className="text-muted w-14 text-right shrink-0 hover:text-ink"
          onClick={() => {
            setArrive(stop.arrive ?? '')
            setEditing('arrive')
          }}
          title="修改到达时间"
        >
          {stop.arrive ?? '--:--'}
        </button>
      )}

      {/* 逗留时长（可点编辑） */}
      {editing === 'stay' ? (
        <input
          className="w-16 border border-ink rounded-[2px] px-1 text-[12px] outline-none"
          value={stay}
          onChange={(e) => setStay(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === 'Enter' && commit()}
          autoFocus
        />
      ) : (
        <button
          className="text-muted w-16 text-right shrink-0 hover:text-ink"
          onClick={() => {
            setStay(String(stop.stay_min))
            setEditing('stay')
          }}
          title="修改逗留时长（分钟）"
        >
          {stop.stay_min}分
        </button>
      )}

      <span className="text-muted w-20 text-right shrink-0">
        {stop.from_prev_km > 0 ? `${stop.from_prev_km} km` : '出发地'}
      </span>

      <span className="flex items-center gap-1 shrink-0">
        {stop.checks.map((c, i) => (
          <CheckBadge key={i} status={c.status} />
        ))}
      </span>

      <button
        className="w-6 h-6 shrink-0 text-muted hover:text-status-fail opacity-0 group-hover:opacity-100 transition-opacity"
        onClick={() => onDelete(day, stop.seq)}
        title="删除该站"
      >
        ✕
      </button>
    </div>
  )
}

/* ── 单天容器（整体是可投落目标：拖到空白处 = 插到末尾）────── */
function DayColumn({
  day,
  stops,
  dragging,
  onDelete,
  onCommitTime,
}: {
  day: number
  stops: Stop[]
  dragging: boolean
  onDelete: (day: number, seq: number) => void
  onCommitTime: (day: number, seq: number, arrive: string | null, stayMin: number | null) => void
}) {
  const { setNodeRef, isOver } = useDroppable({
    id: `day-${day}`,
    data: { day, isDayDrop: true },
  })
  return (
    <div
      ref={setNodeRef}
      className={[
        'border border-ink rounded-[4px] p-3 bg-white',
        isOver && !dragging ? 'outline outline-2 outline-status-pass -outline-offset-2' : '',
      ].join(' ')}
    >
      <div className="font-display font-bold text-sm mb-1 flex items-center gap-2">
        DAY {day}
        <span className="text-[11px] font-normal text-muted">{stops.length} 站</span>
      </div>
      {stops.length === 0 ? (
        <div className="text-[12px] text-muted py-2 text-center border border-dashed border-ink/40 rounded-[4px]">
          拖入站点到这一天
        </div>
      ) : (
        stops.map((s) => (
          <DraggableStopRow
            key={s.seq}
            stop={s}
            day={day}
            dragging={dragging}
            onDelete={onDelete}
            onCommitTime={onCommitTime}
          />
        ))
      )}
    </div>
  )
}

/* ── 拖拽中的浮层副本 ───────────────────────────────────── */
function DragPreview({ name }: { name: string | null }) {
  if (!name) return null
  return (
    <div className="border border-ink bg-pop-yellow rounded-[4px] px-3 py-2 text-[13px] font-bold shadow-none">
      {name}
    </div>
  )
}

/**
 * 站点表格：dnd-kit 拖拽排序（支持跨天），删站，改时间。
 *
 * 🔴 前端只提交 op、只渲染后端返回的整份 Trip，不做本地重算。
 * 🔴 to_seq 是「插入」不是「交换」（1-based），目标天原有站顺延。
 * 🔴 跨天 move 带 to_day；同日内 move 不带（null）。
 */
export default function TripTable({
  trip,
  busy,
  onCommit,
}: {
  trip: Trip
  busy: boolean
  onCommit: (ops: TripOp[]) => void
}) {
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }))
  const [activeStop, setActiveStop] = useState<{ day: number; seq: number; name: string } | null>(null)

  const onDragStart = (e: DragStartEvent) => {
    const d = e.active.data.current as
      | { day?: number; seq?: number; name?: string; isStop?: boolean }
      | undefined
    if (d?.isStop) setActiveStop({ day: d.day!, seq: d.seq!, name: d.name ?? '' })
  }

  const onDragEnd = (e: DragEndEvent) => {
    const active = e.active.data.current as
      | { day?: number; seq?: number; isStop?: boolean }
      | undefined
    const over = e.over?.data.current as
      | { day?: number; seq?: number; isStopDrop?: boolean; isDayDrop?: boolean }
      | undefined
    setActiveStop(null)
    if (busy || !active?.isStop || !over) return

    const fromDay = active.day!
    const fromSeq = active.seq!
    const toDay = over.day!
    if (fromDay === toDay && over.isStopDrop && over.seq === fromSeq) return

    // 目标天 stops（不含被拖站）
    const targetStops = trip.days.find((d) => d.day === toDay)?.stops ?? []
    const others = targetStops.filter((s) => !(fromDay === toDay && s.seq === fromSeq))
    // over 是某行 → 插入该行前；over 是天容器空白 → 插到末尾
    const insertIndex = over.isStopDrop
      ? others.filter((s) => s.seq < over.seq!).length
      : others.length
    const toSeq = insertIndex + 1

    const op: TripOp = {
      op: 'move',
      day: fromDay,
      seq: fromSeq,
      to_seq: toSeq,
      to_day: toDay !== fromDay ? toDay : null,
      arrive: null,
      stay_min: null,
    }
    onCommit([op])
  }

  return (
    <DndContext sensors={sensors} onDragStart={onDragStart} onDragEnd={onDragEnd}>
      <div className="grid gap-4" style={{ gridTemplateColumns: `repeat(${trip.days.length}, minmax(0,1fr))` }}>
        {trip.days.map((day) => (
          <DayColumn
            key={day.day}
            day={day.day}
            stops={day.stops}
            dragging={activeStop !== null}
            onDelete={(d, seq) =>
              onCommit([{ op: 'delete', day: d, seq, to_seq: null, to_day: null, arrive: null, stay_min: null }])
            }
            onCommitTime={(d, seq, arrive, stayMin) =>
              onCommit([
                {
                  op: 'update_time',
                  day: d,
                  seq,
                  to_seq: null,
                  to_day: null,
                  arrive,
                  stay_min: stayMin,
                },
              ])
            }
          />
        ))}
      </div>
      <DragOverlay dropAnimation={null}>
        <DragPreview name={activeStop?.name ?? null} />
      </DragOverlay>
    </DndContext>
  )
}
