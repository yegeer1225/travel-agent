"""模型可调的 3 个工具（A7）。细节在工具内部消化，不增加工具数量。

═══════════════════════════════════════════════════════════════
 三个设计决定
═══════════════════════════════════════════════════════════════

**① 工具返回的是「给模型看的文本」，不是 JSON。**

模型读紧凑的表格文本比读 JSON 更省 token、更少格式错误。
但**完整数据不能丢** —— 它被记进 `poi_pool`（见那个模块的说明），
校验层从池子里拿全量字段。这两个通道的分离是整个设计的枢纽：
模型只看到它需要用来决策的字段，机器保留它需要用来校验的全部字段。

**② `calc_distance` 收的是 `poi_id`，不是坐标。**

强制的副作用是好的：模型想算距离，就必须先搜过那两个点。
- 收坐标 → 模型可能凭记忆写一个坐标（编造，且没人能发现）
- 收名字 → 有歧义（「人民公园」全国有几十个）
- ✅ 收 `poi_id` → 池子里有就能算，没有就报错让它先搜 —— **顺带把封闭世界走通了**

**③ 工具描述（docstring）里直接写约束，不放进 system prompt。**

理由：约束写在**它生效的地方**。模型决定要不要调这个工具时读到的是描述，
此时"搜不到也别编"比放在 500 行 system prompt 中间有效得多。
另有一个实际好处：这些描述会随 tool schema 一起进上下文，不存在"prompt 里写了但被截断"。
"""

from __future__ import annotations

from datetime import date, datetime

from langchain_core.tools import BaseTool, tool

from app.providers.base import AmapProvider
from app.schemas import WeatherStatus
from app.tools import poi_pool

MAX_CANDIDATES = 8
"""一次搜索最多给模型看几个候选。

不暴露给模型当参数是**故意的**：让它设 `limit` 只会多一个可以设错的地方，
而它并不知道"多给几个"和"少给几个"哪个对自己的决策更好。
"""


def _parse_date(value: str) -> date:
    """容错解析模型给的日期。

    模型会输出 `2026-09-16`、`2026/09/16`、`20260916` 甚至 `9月16日`。
    只在**明确能解析**时才通过 —— 猜日期比报错危险得多，
    因为猜错会静默地查到另一天的天气，而结果看起来完全正常。
    """
    text = value.strip().replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"日期格式无法识别：{value!r}。请用 YYYY-MM-DD，例如 2026-10-01")


def build_amap_tools(
    provider: AmapProvider,
    *,
    default_city: str = "成都",
) -> list[BaseTool]:
    """造出绑定好数据源的 3 个工具。

    用工厂而不是模块级单例，是为了让 **provider 可替换**（D23）：
    mock 与 real 造出的工具**签名、描述、返回格式完全一致**，
    图那一层完全不知道背后是谁。
    """

    @tool
    async def search_poi(keyword: str, city: str = "") -> str:
        """按关键词搜索地点（景点 / 餐厅 / 酒店 / 商场 / 车站等），返回候选清单。

        返回的每一项都带一个 **id**。本次规划里**所有引用地点的地方都必须用这个 id**，
        不要凭记忆或常识填写任何搜索没返回过的地点。

        Args:
            keyword: 搜索词，例如"武侯祠"、"火锅"、"酒店"。越具体越容易命中。
            city: 城市名，例如"成都"。留空则用本次行程的目的地。
        """
        target_city = city.strip() or default_city
        pois = await provider.search_poi(keyword, city=target_city, limit=MAX_CANDIDATES)

        # ★ 记进池子 —— 这是"模型没编造"能被证明的唯一途径
        poi_pool.record(pois)

        if not pois:
            return (
                f"在 {target_city} 没有找到「{keyword}」的候选地点。\n"
                f"换一个更通用或更常见的关键词再搜一次。\n"
                f"**不要凭记忆填写没有搜索到的地点**；如果实在搜不到，"
                f"就少安排一个站点，而不是编一个出来。"
            )

        lines = [f"{len(pois)} 个候选（关键词「{keyword}」，城市 {target_city}）："]
        for p in pois:
            parts = [p.poi_id, p.name]
            if p.rating:
                parts.append(f"评分{p.rating}")
            parts.append(f"开放{p.open_time}" if p.open_time else "开放时间未知")
            parts.append(f"{p.lng},{p.lat}")
            lines.append(" | ".join(parts))
        return "\n".join(lines)

    @tool
    async def get_weather(city: str = "", date_str: str = "") -> str:
        """查某个城市某一天的天气。

        ⚠️ 只能查到**从今天起 4 天内**的天气。超出范围时会明确告诉你"无法判定"，
        这时**不要自行推测天气**，把该天的天气留空即可。

        Args:
            city: 城市名，例如"成都"。留空则用本次行程的目的地。
            date_str: 日期，格式 YYYY-MM-DD，例如"2026-10-01"。
        """
        target_city = city.strip() or default_city
        try:
            target_date = _parse_date(date_str)
        except ValueError as exc:
            return f"日期参数有问题：{exc}"

        weather = await provider.get_weather(target_city, target_date)

        if weather.status is WeatherStatus.UNAVAILABLE:
            return (
                f"无法判定 {target_city} {target_date:%Y-%m-%d} 的天气。\n"
                f"原因：{weather.note}\n"
                f"这一天的天气字段请留空（null），**不要编造**。"
            )

        return (
            f"{target_city} {target_date:%Y-%m-%d}："
            f"白天 {weather.day_weather}，夜间 {weather.night_weather}；"
            f"气温 {weather.day_temp}~{weather.night_temp}℃；"
            f"{weather.day_wind}风 {weather.day_power} 级"
            f"（预报发布 {weather.report_time}）"
        )

    @tool
    async def calc_distance(from_poi_id: str, to_poi_id: str) -> str:
        """计算两个地点之间的**驾车**距离与耗时。

        Args:
            from_poi_id: 起点 id，必须是 search_poi 返回过的。
            to_poi_id: 终点 id，同样必须来自 search_poi。

        如果提示 id 不在候选池里，说明那个地点你还没搜过 —— 先搜它。
        **不要用坐标或名称代替 id。**
        """
        pool = poi_pool.current_pool()
        missing = [i for i in (from_poi_id, to_poi_id) if not pool or not pool.contains(i)]
        if missing:
            return (
                f"这些 id 不在候选池里：{'、'.join(missing)}。\n"
                f"请先用 search_poi 搜索对应地点，再用它返回的 id 计算距离。\n"
                f"**不要自己推算距离或时间** —— 距离必须来自这个工具。"
            )

        assert pool is not None  # 上面的判断已经保证
        a = pool.get(from_poi_id)
        b = pool.get(to_poi_id)
        assert a is not None and b is not None

        result = await provider.calc_distance((a.lng, a.lat), (b.lng, b.lat))
        return (
            f"{a.name} → {b.name}：驾车 {result.km} km，约 {result.drive_min} 分钟。"
        )

    return [search_poi, get_weather, calc_distance]


__all__ = ["MAX_CANDIDATES", "build_amap_tools"]
