"""mock 数据源 —— **真结构假数据**（D23）。

它存在的唯一目的：让前端（豆包）和 M1 骨架**不用等真 Key、不用联网**就能跑，
且拿到的 JSON 与真数据**同一套 Pydantic 模型**，一个字段都不差。

═══════════════════════════════════════════════════════════════
 数据来源交代（这一段是防止"mock 就是编造"的误解）
═══════════════════════════════════════════════════════════════

| 数据 | 可靠性 |
|---|---|
| 9 个 POI 的 `poi_id` / 名称 / 经纬度 / `rating` / `open_time` | ✅ **2026-09-15 实测抓取的真值**，一个都没改 |
| 天气（白天/夜间/温度/风力） | ✅ 实测抓到的真预报；**日期按"今天"滚动**，以复现"只覆盖 4 天"这个真实约束 |
| 距离 | ⚠️ **haversine 直线距离 × 1.3**（模拟路网绕行）。**不是真驾车距离** —— 这是 mock 与 real 之间唯一"量级对、数值不准"的地方 |
| `typecode` / `photos` / `address` / `adname` / `adcode` / `cost_per_person` | ⬜ **实测未采集 → 一律 `None` / `[]`。不编造** |

最后一行是刻意的：**mock 缺字段比 mock 有假字段安全得多**。
假 `typecode` 会让"景点页类型过滤"在 mock 下看起来能用，接真数据才发现对不上；
而 `None` 会让那段逻辑**现在就走"无数据"分支**，问题当场暴露。

═══════════════════════════════════════════════════════════════
 两个刻意的"不友好"行为（不是 bug）
═══════════════════════════════════════════════════════════════

1. **搜不到就返回空列表**。池子里只有 9 个实测点，模型搜"火锅"会拿到 `[]`。
   这是**故意保留的失败路径** —— 校验层的"搜不到"提示、以及模型遇到空结果时怎么办，
   都属于 M1~M3 要跑通的东西。把 mock 做成"什么都能搜到"会掩盖这些路径。
2. **出发日在 4 天之外时返回 `unavailable`**，而不是硬凑一个预报。
   高德真的就是这样（`WeatherStatus` 契约里写死了），mock 必须同行为。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from app.providers.base import DistanceResult
from app.schemas import AmapPoi, Weather, WeatherStatus

MOCK_CITY = "成都"
"""mock 池里 9 个点全在成都。`search_poi(city=...)` 传别的城市会得到空列表。"""


# ══════════════════════════════════════════════════════════════
#  POI 池 —— 9 个真实点，字段值全部来自 2026-09-15 的高德实测
# ══════════════════════════════════════════════════════════════
# 命名为公开常量（而不是 `_` 前缀的私有名）是**故意的**：
# 测试需要断言"池子里的每一条都满足某条契约"，而按关键词搜只能命中子集。
# 想拿全量的地方（测试 / 调试脚本）直接用这个，**不要**为了拿全量
# 在 `search_poi` 里加"搜空词返回全部"这种口子 —— 那会让"搜不到返回空"
# 这条真实行为（见本文件顶部第 1 条）失效。

MOCK_POI_POOL: tuple[AmapPoi, ...] = (
    AmapPoi(
        poi_id="B001C07VJ2",
        name="成都武侯祠博物馆",
        alias=["武侯祠", "武侯祠博物馆"],
        lng=104.047992,
        lat=30.646168,
        rating="4.8",
        open_time="08:30-18:30",
    ),
    AmapPoi(
        poi_id="B0FFFD3P2C",
        name="锦里古街",
        alias=["锦里"],
        lng=104.049828,
        lat=30.645994,
        rating="4.8",
        open_time="24小时营业",
    ),
    AmapPoi(
        poi_id="B001C7X8QA",
        name="人民公园",
        lng=104.057641,
        lat=30.656990,
        rating="4.8",
        open_time="24小时营业",
    ),
    AmapPoi(
        poi_id="B001C7YCM4",
        name="宽窄巷子景区",
        alias=["宽窄巷子"],
        lng=104.053307,
        lat=30.663869,
        rating="4.8",
        open_time="24小时营业",
    ),
    AmapPoi(
        poi_id="B001C7WE5S",
        name="成都大熊猫繁育研究基地",
        alias=["熊猫基地", "大熊猫基地", "熊猫繁育研究基地"],
        lng=104.138176,
        lat=30.740573,
        rating="4.8",
        # ⚠️ 双时段 = 旺季/淡季两套时间，实测真值就是这样，别当脏数据
        open_time="07:30-17:00 08:00-16:30",
    ),
    AmapPoi(
        poi_id="B0FFF6X49V",
        name="成都太古里",
        alias=["太古里"],
        lng=104.083809,
        lat=30.653358,
        rating="4.9",
        open_time="10:00-22:00",
    ),
    AmapPoi(
        poi_id="B001C8RQLM",
        name="春熙路步行街",
        alias=["春熙路"],
        lng=104.077774,
        lat=30.655544,
        rating="4.9",
        open_time="24小时营业",
    ),
    AmapPoi(
        poi_id="B001C06PA9",
        name="都江堰景区",
        alias=["都江堰"],
        lng=103.610529,
        lat=31.003363,
        rating="4.8",
        open_time="08:00-18:00 08:00-17:30",
    ),
    AmapPoi(
        poi_id="B001C06ESL",
        name="青城山景区",
        alias=["青城山"],
        lng=103.563817,
        lat=30.904400,
        rating="4.7",
        # 🔴 实测高德**没返回**这个 POI 的开放时间 → 保持 None。
        # 这是「三态」里 unknown 的真实来源：校验层据此判 open_today = unknown。
        open_time=None,
    ),
)


# ══════════════════════════════════════════════════════════════
#  天气 —— 实测真预报 + 按"今天"滚动
# ══════════════════════════════════════════════════════════════

_FORECAST_WINDOW_DAYS = 4
"""高德实测给 4 天（含当天），不是文档常说的 3 天。见 `技术方案.md` 一节末。"""

# (白天, 夜间, 白天温度, 夜间温度) —— 前 3 组是 2026-09-15 实测值，
# 第 4 组是 mock 补位（那一天实测脚本没抓到，不假装它是真值）
_FORECAST = (
    ("阴", "阵雨", 23, 17),
    ("阵雨", "多云", 25, 19),
    ("阵雨", "阴", 25, 19),
    ("多云", "晴", 26, 20),
)

_WIND = "北"
_POWER = "1-3"


def _build_weather(day: date, today_at_call: date) -> Weather:
    offset = (day - today_at_call).days

    if offset < 0:
        return Weather(
            status=WeatherStatus.UNAVAILABLE,
            note=f"{day:%Y-%m-%d} 已经过去了，无法作为出发日",
        )

    if offset >= _FORECAST_WINDOW_DAYS:
        # 与真实高德同行为：窗口外不给数据，也**不假装给**。
        # 注意 note 要说人话 —— 它会直接显示给用户（`schemas.py` 的 Weather.note）
        return Weather(
            status=WeatherStatus.UNAVAILABLE,
            note=(
                f"高德只提供从今天起 {_FORECAST_WINDOW_DAYS} 天的预报，"
                f"出发日 {day:%Y-%m-%d} 超出预报窗口，天气无法判定"
            ),
        )

    day_weather, night_weather, day_temp, night_temp = _FORECAST[offset]
    return Weather(
        status=WeatherStatus.OK,
        day_weather=day_weather,
        night_weather=night_weather,
        day_temp=day_temp,
        night_temp=night_temp,
        day_wind=_WIND,
        day_power=_POWER,
        report_time=f"{today_at_call:%Y-%m-%d} 08:00:00",
    )


# ══════════════════════════════════════════════════════════════
#  距离
# ══════════════════════════════════════════════════════════════

_ROAD_FACTOR = 1.3
"""直线 → 路网的绕行系数。城市道路经验值 1.25~1.45，取中偏保守。"""

_AVG_SPEED_KMH = 28.0
"""市区平均车速。用来把距离换算成时间 —— 不引入额外数据源。"""


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """球面直线距离，单位 km。参数顺序 `(lng, lat)`（高德惯例，别和 GeoJSON 搞反）。"""
    R = 6371.0088
    lng1, lat1 = a
    lng2, lat2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


# ══════════════════════════════════════════════════════════════
#  Provider
# ══════════════════════════════════════════════════════════════


class MockAmapProvider:
    """离线数据源。**接口与真实 provider 完全一致**（D23）。"""

    name = "mock"

    def __init__(self, today: date | None = None) -> None:
        # `today` 可注入 —— 让测试能固定"今天"，否则跑一段时间后
        # 天气断言会因为真实日期推进而失效（这类测试是最难查的）
        self._today = today or date.today()

    async def search_poi(
        self,
        keyword: str,
        city: str | None = None,
        limit: int = 10,
    ) -> list[AmapPoi]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        if city and MOCK_CITY not in city:
            return []

        kw = keyword.lower()
        hits: list[AmapPoi] = []
        for poi in MOCK_POI_POOL:
            candidates = [poi.name, *poi.alias]
            # 双向包含：搜"武侯祠"能命中"成都武侯祠博物馆"，
            # 搜"成都武侯祠博物馆"也能命中别名"武侯祠"
            if any(kw in c.lower() or c.lower() in kw for c in candidates):
                hits.append(poi)

        return hits[: max(1, min(limit, 25))]

    async def get_weather(self, city: str, day: date) -> Weather:
        if city and MOCK_CITY not in city:
            return Weather(
                status=WeatherStatus.UNAVAILABLE,
                note=f"mock 池里只有 {MOCK_CITY} 的数据，查不到 {city} 的天气",
            )
        return _build_weather(day, self._today)

    async def calc_distance(
        self,
        origin: tuple[float, float],
        dest: tuple[float, float],
    ) -> DistanceResult:
        straight = _haversine_km(origin, dest)
        km = straight * _ROAD_FACTOR
        minutes = round(km / _AVG_SPEED_KMH * 60)
        return DistanceResult(
            km=round(km, 1),
            drive_min=max(0, minutes),
            straight_km=round(straight, 2),
        )

    def describe(self) -> str:
        """给启动日志用的一句话说明。**日志里必须能看出当前是 mock**，
        否则"数据怎么不对"会查很久。"""
        return (
            f"MockAmapProvider（离线，{len(MOCK_POI_POOL)} 个真实 POI，"
            f"天气窗口 {_FORECAST_WINDOW_DAYS} 天，今天={self._today}）"
        )


__all__ = ["MOCK_CITY", "MOCK_POI_POOL", "MockAmapProvider"]
