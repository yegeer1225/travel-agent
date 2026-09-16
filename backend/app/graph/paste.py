"""热启动（M7）：粘贴现成行程 → 解析 → 逐站搜真 POI → 组装 → 校验 → SSE 事件流。

═══════════════════════════════════════════════════════════════
 为什么不能让 LLM 直接把粘贴文本变成 Trip
═══════════════════════════════════════════════════════════════

LLM 出的 `poi_id` 必然是编的 —— 而 `poi_exists` 硬判据的全部意义就是
「每个 POI 必须是工具真的返回过的」（封闭世界，`poi_pool.py`）。

所以流程拆成两步：
1. **LLM 只做文本 → 地名清单**（它擅长的），**不产 POI id**
2. 每个地名跑 `search_poi` 拿**真 id**（代码做，搜到的自动进候选池）

这样粘贴出来的行程和生成出来的行程站在同一条可信度链路上。

⚠️ 事件流与 chat **同一套契约**（api.md 3.7），差别只有两条：
不发 `session` 事件、`done.session_id` 为 `null`（A37，不建会话）。
节点名**复用 chat 的枚举**（parse_intent/tool_step/...）——
不新增枚举值，前端零感知。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from app.graph.intent import Requirements, Travelers
from app.graph.nodes import Nodes
from app.graph.soft import run_soft_checks
from app.graph.validate import check_poi_exists, flatten_checks, validate_trip
from app.schemas import (
    CheckEvent,
    CheckLevel,
    CheckStatus,
    Day,
    NodeEvent,
    SSEEventType,
    Stop,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    Trip,
    TripEvent,
    TripSource,
    Validation,
    ValidationIssue,
    Weather,
    WeatherStatus,
)
from app.tools.poi_pool import PoiPool

# 一天的起始时刻。粘贴的文本几乎没有精确到时刻的行程，代码顺推。
# （生成场景的 arrive 是模型给的；这里没有"模型"，就取一个常规值。）
DAY_START = "09:00"
DEFAULT_STAY_MIN = 90
WALK_KM_PER_STOP = 1.2  # 与 draft.py 同一个估算口径（每站步行 ~1.2 km）

# 每天最多 10 站：粘贴的"一天"经常是流水账，不给上限 LLM 会塞 30 个地名
MAX_STOPS_PER_DAY = 10
MAX_DAYS = 10


# ══════════════════════════════════════════════════════════════
#  第一步：LLM 解析 —— 文本 → 地名清单（内部模型，非 API 契约）
# ══════════════════════════════════════════════════════════════


class PasteStopRef(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    stay_min: int = Field(default=DEFAULT_STAY_MIN, ge=10, le=480)


class PasteDayRef(BaseModel):
    theme: str | None = None
    stops: list[PasteStopRef] = Field(min_length=1, max_length=MAX_STOPS_PER_DAY)


class PastePlan(BaseModel):
    """LLM 的解析目标。**只有地名，没有 POI id** —— id 必须来自搜索。

    `travelers`：粘贴文本里写了"带爸妈/带小孩"就抽出来 —— **判据要看它**：
    `walk_load`/车程上限在带长辈时阈值更严（8km→5km）。抽不出来 = None = 按无同行人，
    与"文本没提"的事实一致（不猜）。
    """

    destination: str | None = None
    title: str | None = Field(default=None, max_length=60)
    travelers: Travelers | None = None
    days: list[PasteDayRef] = Field(min_length=1, max_length=MAX_DAYS)


PARSE_SYSTEM_PROMPT = """你是旅游行程解析器。用户会粘贴一段现成的行程/攻略文本，\
把它解析成结构化的「天 → 站点地名清单」。

规则：
1. 只提取**地点名**（景点/餐厅/商圈），不要编造文本里没有的地点
2. 按原文的天数分组；原文没有分天就按合理顺序放进同一天
3. destination = 行程所在城市（正文能看出来就填，看不出来填 null）
4. title = 给这份行程起一个 20 字以内的标题（基于原文内容，不要夸张）
5. stay_min = 每站建议停留分钟数；原文说了就用原文，没说用 90
6. travelers = 同行人构成（adults/children/elders 三个数字 + note）：\
原文提到"带爸妈/父母/老人"→ elders 至少 1；"带小孩/孩子"→ children 至少 1；\
没提到的一律 null，**不要猜**

只输出 JSON：
{"destination": "城市名或null", "title": "标题", "travelers": {"adults": 2, "children": null, "elders": null, "note": null}或null, \
"days": [{"theme": "主题或null", "stops": [{"name": "地点名", "stay_min": 90}]}]}"""


def _parse_plan(raw: str) -> PastePlan | None:
    """两级容错（同 `parse_draft`）：先剥 ``` 围栏，再 Pydantic。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return PastePlan.model_validate_json(text)
    except Exception:  # noqa: BLE001 —— 解析失败由调用方决定重试/报错
        return None


async def _ask_llm_json(llm: Any, system: str, user: str) -> str:
    """一次结构化调用（response_format=json_object，与 nodes.parse_intent 同款）。"""
    resp = await llm.bind(response_format={"type": "json_object"}).ainvoke(
        [{"role": "system", "content": system}, {"role": "user", "content": user}]
    )
    return getattr(resp, "content", "") or ""


# ══════════════════════════════════════════════════════════════
#  主流程：一个 async generator，吐与 chat 同款的事件 dict
# ══════════════════════════════════════════════════════════════


def _node(name: str, phase: str, elapsed: int | None = None) -> dict[str, Any]:
    from app.api.chat_stream import NODE_LABELS

    return {"event": NodeEvent(
        type=SSEEventType.NODE, node=name, phase=phase,
        label=NODE_LABELS[name], elapsed_ms=elapsed,
    )}


def _hhmm(minute_of_day: int) -> str:
    """分钟数 → HH:MM。超过 23:59 截断 —— 时间轴溢出本身会被
    `check_day_drive`（当天车程上限）抓出来，不该在这里造非法值。"""
    m = min(max(minute_of_day, 0), 23 * 60 + 59)
    return f"{m // 60:02d}:{m % 60:02d}"


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


async def paste_trip_stream(
    nodes: Nodes,
    *,
    text: str,
    destination: str | None,
    user_id: int = 1,
) -> AsyncIterator[dict[str, Any]]:
    """热启动主流程。产出 `{"event": ...}`；**不发 session/done**（路由的事）。"""
    pool = PoiPool()
    t0 = time.monotonic()

    # ── ① parse_intent：文本 → 地名清单（失败回灌重试一次）──
    yield _node("parse_intent", "start")
    plan: PastePlan | None = None
    last_err = ""
    for _attempt in range(2):
        prompt = f"粘贴的行程文本：\n\n{text[:8000]}"
        if last_err:
            prompt = f"上次输出解析失败（{last_err}）。请修正后重新只输出 JSON。\n\n{prompt}"
        raw = await _ask_llm_json(nodes.llm_extract, PARSE_SYSTEM_PROMPT, prompt)
        plan = _parse_plan(raw)
        if plan is not None:
            break
        last_err = "不是合法 JSON 或字段缺失"
    if plan is None:
        from app.schemas import ErrorEvent

        yield {"event": ErrorEvent(
            type="error", code="paste_parse_failed",
            msg="没能从这段文字里解析出行程结构，请检查后重试（或换个来源复制）",
        )}
        return
    yield _node("parse_intent", "end", int((time.monotonic() - t0) * 1000))

    # 目的地兜底链：请求参数 → LLM 解析 → 首个 POI 的 cityname（搜完后回填）
    city = (destination or plan.destination or "").strip() or None

    # ── ② tool_step：逐站搜真 POI（封闭世界的入口 —— 手动 record 进池）──
    yield _node("tool_step", "start")
    skipped: list[str] = []
    resolved: list[list[tuple[Any, PasteStopRef]]] = []
    for day_idx, day_ref in enumerate(plan.days, start=1):
        resolved_day: list[tuple[Any, PasteStopRef]] = []
        for stop_ref in day_ref.stops:
            call_id = f"paste-{day_idx}-{len(resolved_day) + 1}"
            yield {"event": ToolCallEvent(
                type=SSEEventType.TOOL_CALL, call_id=call_id, tool="search_poi",
                args={"keyword": stop_ref.name, "city": city},
                label=f"正在搜索：{stop_ref.name}",
            )}
            try:
                results = await nodes.provider.search_poi(stop_ref.name, city=city, limit=5)
            except Exception as exc:  # noqa: BLE001 —— 单站搜索失败不拖垮整份行程
                results = []
                yield {"event": ToolResultEvent(
                    type=SSEEventType.TOOL_RESULT, call_id=call_id, tool="search_poi",
                    ok=False, summary=f"搜索失败：{type(exc).__name__}", degraded=False,
                )}
                skipped.append(stop_ref.name)
                continue
            pool.record(results)
            poi = results[0] if results else None
            if poi is None:
                # 「搜不到」是正常业务结果（不是错）—— 跳过并留痕，绝不编一个
                yield {"event": ToolResultEvent(
                    type=SSEEventType.TOOL_RESULT, call_id=call_id, tool="search_poi",
                    ok=True, summary=f"「{stop_ref.name}」没有搜到，已跳过", degraded=False,
                )}
                skipped.append(stop_ref.name)
                continue
            city = city or poi.cityname or None  # 从真数据回填城市（契约 3.4 的兜底）
            yield {"event": ToolResultEvent(
                type=SSEEventType.TOOL_RESULT, call_id=call_id, tool="search_poi",
                ok=True, summary=f"采用：{poi.name}", degraded=False,
            )}
            resolved_day.append((poi, stop_ref))
        if resolved_day:
            resolved.append(resolved_day)
    yield _node("tool_step", "end", int((time.monotonic() - t0) * 1000))

    if not resolved:
        from app.schemas import ErrorEvent

        yield {"event": ErrorEvent(
            type="error", code="paste_no_pois",
            msg="文中的地点一个都没有搜到，无法生成行程",
        )}
        return

    # ── ③ generate_plan：组装 Trip（纯代码，瞬间）──
    yield _node("generate_plan", "start")
    now = datetime.now(timezone.utc)
    days: list[Day] = []
    day_cursor = _minutes(DAY_START)
    total_cost: float | None = None
    for day_idx, resolved_day in enumerate(resolved, start=1):
        stops: list[Stop] = []
        minute = day_cursor
        day_km = 0.0
        day_drive = 0
        prev_coord: tuple[float, float] | None = None
        for seq, (poi, ref) in enumerate(resolved_day, start=1):
            km, drive = 0.0, 0
            if prev_coord is not None:
                dist = await nodes.provider.calc_distance(prev_coord, (poi.lng, poi.lat))
                km, drive = dist.km, dist.drive_min
            prev_coord = (poi.lng, poi.lat)
            arrive = _hhmm(minute)
            leave = _hhmm(minute + ref.stay_min)
            minute += ref.stay_min + drive
            day_km += km
            day_drive += drive
            if poi.cost_per_person is not None:
                total_cost = (total_cost or 0.0) + poi.cost_per_person
            stops.append(Stop(
                seq=seq, name=poi.name, poi_id=poi.poi_id, lng=poi.lng, lat=poi.lat,
                arrive=arrive, stay_min=ref.stay_min, leave=leave,
                from_prev_km=km, from_prev_drive_min=drive,
                cost_per_person=poi.cost_per_person, rating=poi.rating,
                open_time=poi.open_time,
                checks=[check_poi_exists(poi.poi_id, pool)],
            ))
        days.append(Day(
            day=day_idx, date=None,  # 粘贴的行程常见无日期，前端必须能渲染"无日期"（api.md 3.4）
            theme=plan.days[day_idx - 1].theme,
            weather=Weather(status=WeatherStatus.UNAVAILABLE, note="粘贴的行程没有日期，无法查询天气"),
            stops=stops,
            day_stats={"distance_km": round(day_km, 1), "drive_min": day_drive,
                       "walk_km": round(len(stops) * WALK_KM_PER_STOP, 1)},
        ))
    trip = Trip(
        trip_id=str(uuid4()),
        session_id=None,  # A37：粘贴不建会话，不能回助手页接着聊
        user_id=user_id,
        title=plan.title or f"{city or '导入'}行程",
        destination=city or "未识别城市",
        source=TripSource.PASTED,
        created_at=now,
        updated_at=now,
        days=days,
        summary={"total_distance_km": round(sum(d.day_stats.distance_km for d in days if d.day_stats), 1),
                 "total_cost_per_person": round(total_cost, 2) if total_cost is not None else None,
                 "stop_count": sum(len(d.stops) for d in days),
                 "hard_errors": 0, "soft_warnings": 0},
        validation=Validation(),
    )
    yield _node("generate_plan", "end", int((time.monotonic() - t0) * 1000))

    # ── ④ check_plan：硬判据（pool 就是本次搜索攒的）──
    yield _node("check_plan", "start")
    # travelers 来自粘贴文本的抽取 —— 不传的话"带爸妈"的行程按 8km 步行上限判，
    # 长辈更严的 5km 阈值永远不生效（M8 评测 demo case 抓到的缺口）
    req = Requirements(travelers=plan.travelers).with_defaults()
    report = await validate_trip(trip, pool, req, distance_fn=nodes.provider.calc_distance)
    trip.summary.hard_errors = len(report.blocking)
    trip.validation.remaining = [
        ValidationIssue(code="paste_skipped", msg=f"「{name}」没有搜到，已跳过")
        for name in skipped
    ] + [ValidationIssue(code="hard_error", msg=m, level=None) for m in report.blocking]
    yield _node("check_plan", "end", int((time.monotonic() - t0) * 1000))

    # ── ⑤ soft_check：软提醒（一次 LLM）──
    yield _node("soft_check", "start")
    await run_soft_checks(trip, req, nodes.llm_soft)
    # ⚠️ 软判据挂**三层**（站/天/行程顶层，LEVEL_OF 决定）—— 计数必须扫全树。
    # 只扫 trip.checks 会漏站级 fail（与 recheck 是同一个坑，坑 14 第二次出现）。
    trip.summary.soft_warnings = sum(
        1 for c in flatten_checks(trip.model_dump(mode="json"))
        if c.level is CheckLevel.SOFT and c.status is CheckStatus.FAILED
    )
    yield _node("soft_check", "end", int((time.monotonic() - t0) * 1000))
    yield {"event": CheckEvent(
        type=SSEEventType.CHECK, round=1,
        hard_errors=len(report.blocking),
        soft_warnings=trip.summary.soft_warnings,
        checks=flatten_checks(trip.model_dump(mode="json")),
    )}

    # ── ⑥ render：总结模板（不为客套话多花一次 LLM，与 chat 同理）──
    yield _node("render", "start")
    yield _node("render", "end", 0)
    text_out = (
        f"行程已导入：{trip.destination} {len(days)} 天 {trip.summary.stop_count} 站，"
        f"共 {trip.summary.hard_errors} 个硬错误。"
    )
    if skipped:
        text_out += f"另有 {len(skipped)} 个地点没有搜到，已跳过（见校验卡）。"
    yield {"event": TokenEvent(type=SSEEventType.TOKEN, text=text_out)}
    yield {"event": TripEvent(type=SSEEventType.TRIP, trip=trip)}


__all__ = ["paste_trip_stream"]
