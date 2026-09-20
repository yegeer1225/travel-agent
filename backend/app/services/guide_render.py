"""行程 → 攻略 的**纯函数渲染器**（D61）。

为什么是独立的模块 + 纯函数，两个理由都说清：

**① 零 LLM。** 正文由行程数据直接渲染。若让模型写正文，同一行程发布两次
会得到两篇不一样的文章 —— 不可复现，也没法写测试。渲染器是确定性的：
同样的 `Trip` 进，同样的字出来，可以单测、可以 diff。

**② 零 IO。** 不收 provider、不查库、不读文件。输入一个 `Trip`，输出一个
`RenderedGuide`。封面图要网络（高德 `photos`），**所以不在这里做** ——
由路由层取好再落库（拿不到就是 `None`，属"缺就隐藏"，绝不阻塞发布）。
一旦这里有 IO，"同一份行程渲染两次结果必须一致"这条不变式就没了。

**③ 正文里不写 `checks`。** 攻略是给人看的行程介绍，不是校验报告。
校验三态是界面二的视觉语言（内部设计规范 2.1），不属于社区文章的内容 ——
把红标写进攻略，等于把"这站有风险"当文章正文发出去。

样式约定：距离一位小数（`38.2 km`），时间用半角连字符 `–`（`09:00–10:30`），
货币用 `¥` 且消费取整（`人均 ¥73`，因为它本身就是"人均消费"的粗粒度数字）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas import Day, DayStats, Stop, Trip

_MAX_TITLE = 100
"""`guides.title` 的列宽。⚠️ `trips.title` 是 200 —— 行程标题**可能超宽**，
不截断会在 INSERT 处炸 `Data too long`，所以这里必须收口。"""

_EMPTY_DAY = "*（这一天空着，没有安排站点）*"
_FOOTER = (
    "*本攻略由「AI 旅游规划助手」根据行程自动生成。"
    "攻略是发布当时的快照 —— 之后修改行程不会同步到这里。*"
)


@dataclass(frozen=True)
class RenderedGuide:
    """渲染结果，字段与 `guides` 表要落库的列一一对应（`cover` 除外，见模块注释）。"""

    title: str
    content_md: str
    poi_ids: list[str]
    destination: str | None


def _fmt_km(km: float) -> str:
    """统一一位小数 —— 整数也带 `.0`，一列数字看着对齐。"""
    return f"{km:.1f}"


def _clip(text: str) -> str:
    """折行 + 截到列宽。行程标题里出现换行会直接破坏 markdown 结构。"""
    text = " ".join(text.split())
    if len(text) <= _MAX_TITLE:
        return text
    return text[: _MAX_TITLE - 1] + "…"


def _meta_line(trip: Trip) -> str:
    """`> 由 AI 路线规划生成 · 2 天 6 站 · 全程 38.2 km · 人均约 ¥120`

    每一项都**有值才写**：`total_cost_per_person` 为 `None` 表示"全行程没有
    一个站点有消费数据"，这时写"人均 ¥0"是编的（契约：缺就隐藏，不填占位）。
    """
    s = trip.summary
    parts = ["由 AI 路线规划生成", f"{len(trip.days)} 天 {s.stop_count} 站"]
    if s.total_distance_km > 0:
        parts.append(f"全程 {_fmt_km(s.total_distance_km)} km")
    if s.total_cost_per_person is not None:
        parts.append(f"人均约 ¥{s.total_cost_per_person:.0f}")
    return "> " + " · ".join(parts)


def _stop_block(stop: Stop, index: int) -> list[str]:
    """一站渲染成三到四行。空字段整行不出现，不留"暂无"。"""
    out = [f"{index}. **{stop.name}**"]

    detail: list[str] = []
    if stop.arrive and stop.leave:
        detail.append(f"{stop.arrive}–{stop.leave}")
    elif stop.arrive:
        detail.append(f"{stop.arrive} 起")
    if stop.stay_min:
        detail.append(f"停留 {stop.stay_min} 分钟")
    if stop.cost_per_person is not None:
        detail.append(f"人均 ¥{stop.cost_per_person:.0f}")
    if stop.rating:
        detail.append(f"评分 {stop.rating}")
    if detail:
        out.append("   " + " · ".join(detail))

    transport: list[str] = []
    if stop.from_prev_km > 0:
        transport.append(f"距上一站 {_fmt_km(stop.from_prev_km)} km")
    if stop.from_prev_drive_min > 0:
        transport.append(f"车程 {stop.from_prev_drive_min} 分钟")
    if stop.open_time:
        transport.append(f"营业时间 {stop.open_time}")
    if transport:
        out.append("   " + " · ".join(transport))

    if stop.match_reason:
        out.append(f"   > 为什么选它：{stop.match_reason}")
    out.append(f"   高德 poi_id：{stop.poi_id}")
    return out


def _day_stats_line(stats: DayStats | None) -> str | None:
    if stats is None:
        return None
    parts: list[str] = []
    if stats.distance_km > 0:
        parts.append(f"全天 {_fmt_km(stats.distance_km)} km")
    if stats.walk_km > 0:
        parts.append(f"步行 {_fmt_km(stats.walk_km)} km")
    if stats.drive_min > 0:
        parts.append(f"车程 {stats.drive_min} 分钟")
    return "当天：" + " · ".join(parts) if parts else None


def _day_block(day: Day) -> list[str]:
    head = f"## Day {day.day}"
    if day.theme:
        head += f" · {day.theme}"
    out = [head, ""]

    facts: list[str] = []
    if day.date is not None:
        facts.append(f"日期 {day.date.isoformat()}")
    w = day.weather
    if w is not None and w.day_weather:
        temp = ""
        if w.day_temp is not None and w.night_temp is not None:
            temp = f"（{w.night_temp}~{w.day_temp}°C）"
        facts.append(f"天气 {w.day_weather}{temp}")
    if facts:
        out += [" · ".join(facts), ""]

    if not day.stops:
        out.append(_EMPTY_DAY)
        return out

    for i, stop in enumerate(day.stops, start=1):
        out.extend(_stop_block(stop, i))

    stats_line = _day_stats_line(day.day_stats)
    if stats_line:
        out += ["", stats_line]
    return out


def render_guide_from_trip(
    trip: Trip,
    *,
    title: str | None = None,
    destination: str | None = None,
) -> RenderedGuide:
    """把一条行程渲染成攻略 markdown。

    标题为空时用「目的地 + 行程」兜底 —— `guides.title` 是 NOT NULL，
    渲染器有责任保证永远给出非空标题。

    ⚠️ 塌缩空白要在**判断非空之前**做：`trip.title = "   "` 是 truthy，
    直接 `or` 会得到一个纯空白标题，落库才炸（或更糟：标题栏空着不报错）。
    """
    raw_title = (title or "").strip() or " ".join(trip.title.split()) or f"{trip.destination}行程"
    final_title = _clip(raw_title)
    final_dest = (destination or "").strip() or trip.destination

    poi_ids: list[str] = []
    for day in trip.days:
        for stop in day.stops:
            if stop.poi_id and stop.poi_id not in poi_ids:
                poi_ids.append(stop.poi_id)

    lines: list[str] = [f"# {final_title}", "", _meta_line(trip), ""]
    for day in trip.days:
        lines.extend(_day_block(day))
        lines.append("")
    lines += ["---", "", _FOOTER]

    return RenderedGuide(
        title=final_title,
        content_md="\n".join(lines).rstrip() + "\n",
        poi_ids=poi_ids,
        destination=final_dest,
    )


__all__ = ["RenderedGuide", "render_guide_from_trip"]
