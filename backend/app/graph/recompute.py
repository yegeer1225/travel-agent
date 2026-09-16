"""确定性重算（M7）：PATCH 的 op 应用 + 距离/时间轴/统计/硬判据重算。

═══════════════════════════════════════════════════════════════
 纪律：这一层**绝不调 LLM**（api.md 3.2）
═══════════════════════════════════════════════════════════════

界面二的每一次拖拽都走这里 —— LLM 的延迟和成本会毁掉交互，而且
"改顺序"根本不需要智能，需要的是**算得准**。软判据不动（顺序相关的
结论在 `recheck` 时才更新）。

两段式（apply → recompute）是有意的：

1. `apply_ops`：只动**结构**（谁在哪天的第几位），逐 op 按序应用，
   找不到目标站 → 整批拒绝（不产生半应用状态）
2. `recompute_trip`：结构定了之后，一次性重算所有**数值**
   （距离 / 时间轴 / day_stats / summary / 跨天天气 / 硬判据）

时间轴的顺推规则（D49 定死）：
- 起点 = 当天首站当前 `arrive`（`update_time` 显式设置过则用设置值）
- 之后每站 `arrive[i] = leave[i-1] + from_prev_drive_min[i]`，`leave = arrive + stay_min`
- 顺推超过 23:59 截断 —— 溢出本身是"这天排太满"的信号，硬判据会抓，不在这里造非法值
- **不 wrap、不"回到次日"** —— 那是校验层该提醒的事
"""

from __future__ import annotations

from datetime import date

from app.graph.intent import Requirements
from app.graph.validate import ValidationReport, validate_trip
from app.schemas import Stop, Trip, Weather, WeatherStatus
from app.tools.poi_pool import PoiPool

DAY_START = "09:00"
WALK_KM_PER_STOP = 1.2  # 与 draft.py / paste.py 同一个估算口径


class OpApplyError(ValueError):
    """op 引用了不存在的天/站，或语义无效（如 update_time 两个值都没给）。"""

    def __init__(self, msg: str) -> None:
        super().__init__(msg)


# ══════════════════════════════════════════════════════════════
#  第一步：结构应用
# ══════════════════════════════════════════════════════════════


def _day_of(trip: Trip, day_no: int):
    for day in trip.days:
        if day.day == day_no:
            return day
    raise OpApplyError(f"第 {day_no} 天不存在")


def _take(day, seq: int) -> Stop:
    """按 seq 取出站（从列表里移除）。seq 是 1-based。"""
    for i, stop in enumerate(day.stops):
        if stop.seq == seq:
            return day.stops.pop(i)
    raise OpApplyError(f"第 {day.day} 天没有第 {seq} 站")


def _renumber(day) -> None:
    for i, stop in enumerate(day.stops, start=1):
        stop.seq = i


def apply_ops(trip: Trip, ops: list) -> tuple[set[int], dict[str, str]]:
    """按序应用一批 op，**只动结构不动数值**。

    返回 `(跨天 move 涉及的天号, 显式 arrive 锚点)`：
    - 天号集合：这些天的天气依赖日期，重算时要重查（D44）；同日重排不在其中
    - 锚点：`update_time` 设置的 arrive，**键 = 站的 `poi_id`**（封闭世界身份，
      同批后续 op 重排 seq 也不会错位；一个 POI 在行程里只出现一次）
    op 引用不存在的天/站 → 抛 `OpApplyError`，调用方整批拒绝。
    """
    touched_cross: set[int] = set()
    pinned_arrive: dict[str, str] = {}

    for op in ops:
        source = _day_of(trip, op.day)

        if op.op == "delete":
            _take(source, op.seq)
            _renumber(source)

        elif op.op == "update_time":
            if op.arrive is None and op.stay_min is None:
                raise OpApplyError("update_time 至少要给 arrive 或 stay_min")
            target = next((s for s in source.stops if s.seq == op.seq), None)
            if target is None:
                raise OpApplyError(f"第 {op.day} 天没有第 {op.seq} 站")
            if op.stay_min is not None:
                target.stay_min = op.stay_min
            if op.arrive is not None:
                target.arrive = op.arrive
                pinned_arrive[target.poi_id] = op.arrive  # 显式锚点，顺推以它为准

        elif op.op == "move":
            moved = _take(source, op.seq)
            _renumber(source)
            if op.to_day is not None and op.to_day != op.day:
                target = _day_of(trip, op.to_day)
                touched_cross.update({op.day, op.to_day})
            else:
                target = source
            if op.to_seq is None:
                raise OpApplyError("move 必须给 to_seq")
            # ⚠️ 插入语义，不是交换（api.md 3.2）：移出后的列表里插到第 to_seq 位
            index = min(max(op.to_seq - 1, 0), len(target.stops))
            target.stops.insert(index, moved)
            _renumber(target)
        else:  # pragma: no cover —— TripOpKind 枚举挡住了，防御性分支
            raise OpApplyError(f"未知 op：{op.op}")

    return touched_cross, pinned_arrive


# ══════════════════════════════════════════════════════════════
#  第二步：数值重算
# ══════════════════════════════════════════════════════════════


def pool_from_trip(trip: Trip) -> PoiPool:
    """从行程自身重建候选池。

    行程里的每站在生成/粘贴时都**已经过真实搜索**（封闭世界闸门放行过的），
    重排不改变这个事实 —— `poi_exists` 在 PATCH 后恒 pass 是**预期行为**，
    不是校验失效。其余判据（open_today / hop / 车程 / 天气）照常真算。
    """
    pool = PoiPool()
    pool.record([
        poi for d in trip.days for poi in (
            _stop_as_poi(s) for s in d.stops
        )
    ])
    return pool


def _stop_as_poi(stop: Stop):
    from app.schemas import AmapPoi

    return AmapPoi(
        poi_id=stop.poi_id, name=stop.name, lng=stop.lng, lat=stop.lat,
        cost_per_person=stop.cost_per_person, rating=stop.rating, open_time=stop.open_time,
    )


def _hhmm(minute_of_day: int) -> str:
    m = min(max(minute_of_day, 0), 23 * 60 + 59)
    return f"{m // 60:02d}:{m % 60:02d}"


def _minutes(hhmm: str | None) -> int | None:
    if not hhmm:
        return None
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


async def recompute_trip(
    trip: Trip,
    *,
    provider,
    requirements: Requirements,
    weather_days: set[int] | None = None,
    pinned_arrive: dict[str, str] | None = None,
) -> ValidationReport:
    """结构定完之后的一次性重算。**原地改 trip**，返回硬校验报告。

    `weather_days` = 需要重查天气的天号（跨天 move 的进/出两天，D44）；
    同日重排传 None / 空 —— 天气跟顺序无关，跟**日期**才有关。
    `pinned_arrive` = `apply_ops` 返回的显式锚点（poi_id → arrive）。
    """
    pinned: dict[str, str] = pinned_arrive or {}

    # ── 1. 距离 + 时间轴：逐天算 ──
    total_cost: float | None = None
    for day in trip.days:
        day_km, day_drive = 0.0, 0
        prev_coord: tuple[float, float] | None = None
        leave_minute: int | None = None  # 上一站 leave；首站前为 None
        for seq, stop in enumerate(day.stops, start=1):
            # 距离：首站 from_prev=0（D43 口径 —— 每天从第一站起算）
            km, drive = 0.0, 0
            if prev_coord is not None:
                dist = await provider.calc_distance(prev_coord, (stop.lng, stop.lat))
                km, drive = dist.km, dist.drive_min
            stop.from_prev_km, stop.from_prev_drive_min = km, drive
            day_km += km
            day_drive += drive
            prev_coord = (stop.lng, stop.lat)

            # 到达时刻：起点 = 首站当前 arrive（update_time 显式设过用设置值）；
            # 之后 arrive[i] = leave[i-1] + from_prev_drive_min[i]，显式锚点优先
            pinned_a = pinned.get(stop.poi_id)
            if seq == 1:
                start = pinned_a or stop.arrive or DAY_START
                arrive_minute = _minutes(start)
                if arrive_minute is None:
                    arrive_minute = 540  # DAY_START， arrive 不是合法 HHMM 时的兜底
            else:
                assert leave_minute is not None
                arrive_minute = leave_minute + drive
                if pinned_a is not None:
                    arrive_minute = _minutes(pinned_a) or arrive_minute
            stop.arrive = _hhmm(arrive_minute)
            stop.leave = _hhmm(arrive_minute + stop.stay_min)
            leave_minute = arrive_minute + stop.stay_min

            if stop.cost_per_person is not None:
                total_cost = (total_cost or 0.0) + stop.cost_per_person

        # day_stats（与 draft.py 同口径）
        if day.stops:
            from app.schemas import DayStats

            day.day_stats = DayStats(
                distance_km=round(day_km, 1), drive_min=day_drive,
                walk_km=round(len(day.stops) * WALK_KM_PER_STOP, 1),
            )
        else:
            day.day_stats = None

    # ── 2. 跨天天气（D44：被拖入/拖出的天，日期换了天气就换了）──
    for day_no in weather_days or set():
        day = next((d for d in trip.days if d.day == day_no), None)
        if day is None:
            continue
        if day.date is None:
            day.weather = Weather(status=WeatherStatus.UNAVAILABLE,
                                  note="这一天没有明确日期，无法查询天气")
        else:
            day.weather = await provider.get_weather(trip.destination, day.date)

    # ── 3. summary（D43 口径：各天首站 from_prev=0 → 总里程从每天第一站起算）──
    total_km = round(sum(d.day_stats.distance_km for d in trip.days if d.day_stats), 1)
    trip.summary.total_distance_km = total_km
    trip.summary.total_cost_per_person = round(total_cost, 2) if total_cost is not None else None
    trip.summary.stop_count = sum(len(d.stops) for d in trip.days)

    # ── 4. 硬判据（validate_trip 先清空再重填，幂等；distance_fn 传上 → 跨天段也判）──
    report = await validate_trip(
        trip, pool_from_trip(trip), requirements, distance_fn=provider.calc_distance,
    )
    trip.summary.hard_errors = len(report.blocking)
    return report
