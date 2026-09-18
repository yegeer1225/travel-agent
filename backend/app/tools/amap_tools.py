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
from app.tools.poi_rank import label_of, rank_pois
from app.utils import haversine_km

MAX_CANDIDATES = 8
"""一次搜索最多给模型看几个候选。

不暴露给模型当参数是**故意的**：让它设 `limit` 只会多一个可以设错的地方，
而它并不知道"多给几个"和"少给几个"哪个对自己的决策更好。
"""

SEARCH_FETCH = 20
"""**内部**取回多少条（模型看不到这个数）。

为什么不直接取 `MAX_CANDIDATES` 条：实测搜"都江堰"时前 8 条里只有 1 条是景点
（其余是市政府 / 客运中心 / 快铁站 / 道路名），取 8 条就**没得挑**了。
多取的这 12 条**不进 prompt**（token 成本一点不变），
只用来让"把能当站点的挑到前排"这件事有材料可用 —— 见 `poi_rank`。

⚠️ 高德 `offset` 文档上限 25，取 20 是**不贴边界**的值。"""


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


def _search_center() -> str | None:
    """给这次搜索算一个**排序中心点**（`"lng,lat"`），拿不到就返回 `None`（D75）。

    ── 为什么需要 ──────────────────────────────────────────────
    高德不给中心点时，泛关键词的落点由它自己决定。实测成都搜「公园」
    返回的全是高新区那批（中位距市中心 **10.7 km**）；给个市中心附近的坐标后
    变成青羊区的（**2.2 km**）。搜「博物馆」时成都博物馆从**第 9 名**跳到**第 1 名**。

    ── 中心点从哪来 ────────────────────────────────────────────
    **用已搜到的 POI**（池子里的）—— 零额外请求、零硬编码地名。
    不能用的两条路（都实测否证过，见 D75）：
    · `geocode("成都")` → 市政府驻地（2010 年搬到高新区）→ 返回的**还是**高新区那批
    · 硬编码"天府广场" → 换个城市就废

    ── 为什么是 medoid 而不是质心 ──────────────────────────────
    质心会被远郊景点拉到中间地带。实测锚点取「宽窄巷子 + 都江堰」时，
    质心落在**郫都区**（到两边各 28 km）→ 搜「公园」返回郫都区的公园，**比不加还差**。
    medoid（到其余点距离之和最小的那个点）**一定落在某个真实锚点上**，天然抗离群：
    同样的三个锚点（市区两个 + 都江堰一个）→ 选中宽窄巷子 → 返回市中心公园（0.8~2.6 km）。

    平局时 `min` 返回**先出现的**那个 = 先搜到的锚点，而先搜到的通常是主景点 ——
    实测「宽窄巷子 + 都江堰」两点平局时正好选中宽窄巷子，结果是对的。

    ⚠️ 它是**排序权重不是硬过滤**，所以偏了也只是"排序变差"，不会"搜不到"。
    """
    pool = poi_pool.current_pool()
    if pool is None:
        return None

    coords = [(float(p.lng), float(p.lat)) for p in pool.all() if p.lng and p.lat]
    if not coords:
        # 第一次搜索时池子是空的 → 不加中心点 = 与改动前行为完全一致
        return None

    best = min(coords, key=lambda a: sum(haversine_km(a, b) for b in coords))
    return f"{best[0]:.6f},{best[1]:.6f}"


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
        """按关键词搜索地点（景点 / 公园 / 博物馆 / 餐厅 / 商场等），返回候选清单。

        返回的每一项都带一个 **id**。本次规划里**所有引用地点的地方都必须用这个 id**，
        不要凭记忆或常识填写任何搜索没返回过的地点。

        ⚠️ 清单**已经按"能不能当行程里的一站"排过序**：前面是景区 / 公园 / 博物馆
        这类可以直接排进去的地点，越往后越可能是车站、售票处、停车场、酒店等服务设施。
        **那些服务设施不是景点，不要排进行程** —— 如果前几条都不合适，
        换一个更具体的关键词重搜，而不是往后翻着挑。

        Args:
            keyword: 搜索词，例如"武侯祠"、"火锅"、"公园"。越具体越容易命中。
            city: 城市名，例如"成都"。留空则用本次行程的目的地。
        """
        target_city = city.strip() or default_city
        # 多取一些（`SEARCH_FETCH`）→ 重排 → 再截断到 `MAX_CANDIDATES`。
        # ⚠️ 中心点必须在 `record` **之前**算 —— 用"此前搜到的"锚点，不含本次结果（D75）。
        pois = await provider.search_poi(
            keyword, city=target_city, limit=SEARCH_FETCH, center=_search_center()
        )

        # ★ 记进池子 —— 这是"模型没编造"能被证明的唯一途径。
        #   记的是**全量**（不是截断后的 8 条）：截断只发生在展示层，
        #   校验层能看到的 `poi_id` 越多，越不会把"其实搜到过"判成"编的"。
        poi_pool.record(pois)

        if not pois:
            return (
                f"在 {target_city} 没有找到「{keyword}」的候选地点。\n"
                f"换一个更通用或更常见的关键词再搜一次。\n"
                f"**不要凭记忆填写没有搜索到的地点**；如果实在搜不到，"
                f"就少安排一个站点，而不是编一个出来。"
            )

        shown = rank_pois(pois)[:MAX_CANDIDATES]
        lines = [f"{len(shown)} 个候选（关键词「{keyword}」，城市 {target_city}）："]
        for p in shown:
            parts = [p.poi_id, p.name]
            label = label_of(p.type)
            if label:
                # 带类型标签的收益**量不出来**（条目没变），但成本是每行 6 个字符。
                # 它让模型知道"第 3 条是地铁站"从而不去选它 —— 零成本保险。
                parts.append(f"[{label}]")
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
