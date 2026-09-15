"""草稿层：模型出「骨架」，代码补「确定性字段」。

═══════════════════════════════════════════════════════════════
 为什么要有这一层（不要跳过这段）
═══════════════════════════════════════════════════════════════

如果直接让模型输出完整的 `Trip`，它会**顺便编出**这些字段：

| 字段 | 模型会怎么做 | 事实 |
|---|---|---|
| `lng` / `lat` | 凭记忆写一个像模像样的坐标 | 应该从 POI 池子里取 |
| `from_prev_km` / `from_prev_drive_min` | 估一个"大约 15 公里" | 应该由测距工具算 |
| `day_stats` / `summary` | 自己加一遍（还算错） | 应该由代码汇总 |
| `rating` / `open_time` | 按常识填"4.8 / 09:00-17:00" | 应该从接口返回值取 |

**而且这些编造是查不出来的** —— 坐标看着对、距离看着合理，
唯一能发现的方式是逐个回查接口，而那正是校验层该自动做的事。

所以切一刀（也是 D3 的落地）：

```
模型负责「判断」：去哪几个点、顺序、待多久、为什么适合这个用户
代码负责「事实」：坐标、距离、汇总、评分、开放时间
```

模型的输出被压到一个**极小的 schema**（`PlanDraft`），
它**没有机会**编造那些字段 —— 因为那些字段根本不在它的输出结构里。

> 这个手法值得记住：**防止模型编造，最有效的办法不是"禁止"，是"不给它那个字段"。**

═══════════════════════════════════════════════════════════════
 `json_object` 模式下的 schema 怎么传
═══════════════════════════════════════════════════════════════

实测（`scripts/probe_llm_structured.py`）：DeepSeek **不支持
`response_format={"type":"json_schema"}`**（直接 400），只支持 `json_object`。
而 `json_object` 只保证"是合法 JSON"，**不保证字段对**。

所以：schema 写进 prompt（`DRAFT_SCHEMA_HINT`），字段正确性靠 `Pydantic` 校验兜底。
"""

from __future__ import annotations

import uuid
from datetime import date as Date
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.providers.base import AmapProvider
from app.schemas import (
    Check,
    CheckLevel,
    CheckStatus,
    CoordSys,
    Day,
    DayStats,
    Stop,
    Trip,
    TripSource,
    Weather,
    WeatherStatus,
)
from app.tools.poi_pool import PoiPool

# ══════════════════════════════════════════════════════════════
#  模型输出结构 —— 刻意保持极小
# ══════════════════════════════════════════════════════════════

WALK_KM_PER_STOP = 1.2
"""站点内步行的**估算**系数（km/站）。

🔴 这是**显式估算，不是实测值** —— 高德**没有**"走了多少公里"这个数据。
`DayStats.walk_km` 是契约里的必填字段，所以现在只能估。

**为什么名字这么长还要写注释**：估算是可以接受的，**假装它是真的**不行。
合格条件是 ① 函数/常量名能让人一眼看出是估算 ② 界面上标"约"
③ 有明确的重审计划。三条缺一条，它就变成了编数据。

**M3 重审**：要么找到真实来源（如按停留时长与场地面积估算），
要么把这个字段从契约里删掉 —— **宁可缺，不要假精确**。
"""


class DraftStop(BaseModel):
    """模型出的"一站"。**注意这里没有坐标、没有距离、没有评分** —— 见模块 docstring。"""

    model_config = ConfigDict(extra="forbid")

    poi_id: str = Field(
        description="必须是 search_poi 返回过的 id。**不要自己编**，也不要用名称代替。"
    )
    arrive: str = Field(description="到达时刻，24 小时制 HH:MM，如 09:30")
    stay_min: int = Field(ge=0, description="停留分钟数")
    match_reason: str = Field(description="一句话说明为什么这一站适合这位用户")


class DraftDay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ⚠️ 标注写成 `Date` 不是笔误，也不是风格：**字段名恰好叫 `date`**，
    # 直接用 `date` 当类型会让 pydantic 抛
    # `PydanticUserError: field name clashing with a type annotation` ——
    # 类命名空间里 `date` 先被字段名占了，注解求值时拿到的是字段而不是类型。
    # `schemas.py` 的 `Day.date` 用的是同一个写法。
    date: Date = Field(description="日期 YYYY-MM-DD")
    theme: str = Field(description="这一天的主题，一句话")
    stops: list[DraftStop] = Field(default_factory=list)


class PlanDraft(BaseModel):
    """生成节点的输出。**只有判断，没有事实。**"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="行程标题，如「成都3日游·带爸妈慢节奏」")
    days: list[DraftDay] = Field(default_factory=list)


DRAFT_SCHEMA_HINT = """
{
  "title": "行程标题",
  "days": [
    {
      "date": "YYYY-MM-DD",
      "theme": "这一天的主题，一句话",
      "stops": [
        {
          "poi_id": "必须是 search_poi 返回过的 id",
          "arrive": "HH:MM（24 小时制，两位数，如 09:30 —— 9:30 不合格）",
          "stay_min": 90,
          "match_reason": "一句话说明为什么这一站适合这位用户"
        }
      ]
    }
  ]
}
""".strip()


# ══════════════════════════════════════════════════════════════
#  补全
# ══════════════════════════════════════════════════════════════


def _normalize_hhmm(raw: str | None) -> str | None:
    """把模型可能写出的各种时刻形式规范成 `HH:MM`。

    `schemas.py` 里 `HHMM` 是**严格** `\\d{2}:\\d{2}`（`9:00` 会直接报错）。
    这个严格是**故意的**（契约要求），但**不能把严格性转嫁给用户** ——
    模型写 `9:00` 是常见的，直接判失败会让用户看到无意义的错误。

    所以：**能在代码里无歧义修好的，就在代码里修**，不要拿 prompt 去赌。
    """
    if not raw:
        return None
    text = str(raw).strip().replace("：", ":").replace(".", ":")
    if ":" not in text:
        # "0930" / "930"
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) in (3, 4):
            text = f"{digits[:-2]}:{digits[-2:]}"
        else:
            return None
    hh, _, mm = text.partition(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


def _add_minutes(hhmm: str, minutes: int) -> str:
    """时刻 + 分钟 → 时刻。**跨天按 24 小时取模**。

    ⚠️ 取模意味着"23:00 待 120 分钟"会得到 `01:00`，看起来像回到了当天凌晨。
    M1 接受这个近似（真实场景里不该排到深夜）；M3 校验层要加"结束时刻不得晚于 23:00"
    这条软判据，从源头避免它发生。
    """
    h, m = (int(x) for x in hhmm.split(":"))
    total = (h * 60 + m + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _check_poi_exists(poi_id: str, pool: PoiPool) -> Check:
    """M1 唯一的硬判据：这个 POI 是不是工具真的返回过。

    **M3 会把 9 类判据补齐**（硬 5 + 软 4）。M1 先只做这一条，
    因为它是"封闭世界"的**唯一必要条件**，也是整条可信度链路的锚点。
    """
    if pool.contains(poi_id):
        return Check(level=CheckLevel.HARD, code="poi_exists", status=CheckStatus.PASSED)
    return Check(
        level=CheckLevel.HARD,
        code="poi_exists",
        status=CheckStatus.FAILED,
        msg=f"POI id `{poi_id}` 不在候选池里 —— 这个地点不是工具搜出来的",
    )


async def assemble_trip(
    draft: PlanDraft,
    pool: PoiPool,
    provider: AmapProvider,
    weather_by_date: dict[str, Weather] | None = None,
    *,
    destination: str,
    session_id: str | None = None,
    user_id: int = 1,
    rounds: int = 0,
) -> tuple[Trip, list[str]]:
    """把草稿补全成完整 `Trip`。

    返回 `(trip, blocking)` —— `blocking` 是**硬错**列表（比如引用了池子里没有的 poi_id）。
    调用方（`check_plan`）据此决定要不要打回重排。
    """
    weather_by_date = weather_by_date or {}
    blocking: list[str] = []
    now = datetime.now(timezone.utc).astimezone()

    days: list[Day] = []
    total_km = 0.0
    total_cost: float | None = None
    stop_count = 0
    soft_warn = 0

    for day_index, draft_day in enumerate(draft.days, start=1):
        stops: list[Stop] = []
        day_km = 0.0
        day_drive = 0
        prev_coord: tuple[float, float] | None = None

        for seq, draft_stop in enumerate(draft_day.stops, start=1):
            poi = pool.get(draft_stop.poi_id)
            if poi is None:
                # 🔴 封闭世界拦下来的东西 —— **不能悄悄跳过**，
                # 静默丢弃会让用户以为"规划成功但少了一站"
                blocking.append(
                    f"第 {day_index} 天第 {seq} 站的 poi_id `{draft_stop.poi_id}` "
                    f"不在候选池里（不是工具搜出来的），已拒绝"
                )
                continue

            arrive = _normalize_hhmm(draft_stop.arrive)
            leave = _add_minutes(arrive, draft_stop.stay_min) if arrive else None

            # 距离/车程：**代码算**。第 1 站没有前一站，保持 0
            km = 0.0
            drive_min = 0
            if prev_coord is not None:
                dist = await provider.calc_distance(prev_coord, (poi.lng, poi.lat))
                km = dist.km
                drive_min = dist.drive_min
            prev_coord = (poi.lng, poi.lat)

            stops.append(
                Stop(
                    seq=seq,
                    name=poi.name,
                    poi_id=poi.poi_id,
                    lng=poi.lng,
                    lat=poi.lat,
                    coord_sys=CoordSys.GCJ02,
                    arrive=arrive,
                    stay_min=draft_stop.stay_min,
                    leave=leave,
                    from_prev_km=km,
                    from_prev_drive_min=drive_min,
                    cost_per_person=poi.cost_per_person,
                    rating=poi.rating,
                    open_time=poi.open_time,
                    match_reason=draft_stop.match_reason,
                    checks=[_check_poi_exists(poi.poi_id, pool)],
                )
            )
            day_km += km
            day_drive += drive_min
            stop_count += 1
            if poi.cost_per_person is not None:
                total_cost = (total_cost or 0.0) + poi.cost_per_person

        # 天气：能从工具拿就拿，拿不到就如实说拿不到（**不编**）
        date_key = draft_day.date.isoformat()
        weather = weather_by_date.get(date_key)
        if weather is None:
            weather = Weather(
                status=WeatherStatus.UNAVAILABLE,
                note="行程生成时没有查到这一天的天气，需要单独查询",
            )

        days.append(
            Day(
                day=day_index,
                date=draft_day.date,
                theme=draft_day.theme,
                weather=weather,
                stops=stops,
                day_stats=DayStats(
                    distance_km=round(day_km, 1),
                    drive_min=day_drive,
                    walk_km=round(len(stops) * WALK_KM_PER_STOP, 1),  # ← 估算，见常量注释
                ),
            )
        )
        total_km += day_km

    trip = Trip(
        trip_id=str(uuid.uuid4()),
        session_id=session_id,
        user_id=user_id,
        title=draft.title or f"{destination}行程",
        destination=destination,
        source=TripSource.GENERATED,
        created_at=now,
        updated_at=now,
        days=days,
        summary={
            "total_distance_km": round(total_km, 1),
            "total_cost_per_person": round(total_cost, 2) if total_cost is not None else None,
            "stop_count": stop_count,
            # ⚠️ `blocking` 到这里已经累计完了（它在本函数里长出来），
            # 所以能直接算 —— 不再需要"先填 0 后面再改"的占位。
            "hard_errors": len(blocking),
            "soft_warnings": soft_warn,  # M1 恒为 0：软判据排在 M3
        },
        validation={"rounds": rounds, "fixed": [], "remaining": []},
    )
    return trip, blocking


def parse_draft(raw_json: str | dict[str, Any]) -> tuple[PlanDraft | None, str | None]:
    """解析模型输出。返回 `(草稿, 错误信息)`。

    **两级容错，顺序不能反**：
    1. 先剥掉 markdown 代码块围栏 —— 模型即使被要求"只输出 JSON"，
       仍有相当概率包一层 ```json。**这是最常见的失败形式，不是"模型不听话"**
    2. 再交给 Pydantic —— 字段错了要让 `ValidationError` 说出来，
       因为那句报错会被**回灌给模型**让它自己修（M3 的 repair 用）
    """
    if isinstance(raw_json, dict):
        payload: Any = raw_json
    else:
        text = (raw_json or "").strip()
        if text.startswith("```"):
            lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
            text = "\n".join(lines).strip()
        import json

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"输出不是合法 JSON：{exc}"

    try:
        return PlanDraft.model_validate(payload), None
    except ValidationError as exc:
        # 只取前几条 —— 全量错误会很长，而回灌给模型时前几条最有用
        errs = "; ".join(
            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
        )
        return None, f"字段不符合要求：{errs}"


__all__ = [
    "DRAFT_SCHEMA_HINT",
    "WALK_KM_PER_STOP",
    "DraftDay",
    "DraftStop",
    "PlanDraft",
    "assemble_trip",
    "parse_draft",
]
