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
| 距离 | ⚠️ **haversine 直线距离 × 分段路网系数**（三段，见下方「距离」一节）。**不是真驾车距离** —— 这是 mock 与 real 之间唯一"量级对、数值不准"的地方 |
| `photos` | ✅ **2026-09-16 实测补采**（真实高德 `/v3/place/text` 返回，每 POI 3 张，`store.is.autonavi.com` 域名）—— 首页 hero 轮播因此有真图；仍不编造，仅补采真值 |
| `typecode` / `address` / `adname` / `adcode` / `cost_per_person` | ⬜ **实测未采集 → 一律 `None` / `[]`。不编造** |

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
#
# `type` / `typecode` 是 2026-09-15 补采的（`probe_amap.py --collect-mock`）：
# 按 `poi_id` **精确匹配**拿的真值，不是按名字搜出来的（「人民公园」全国有很多个）。
# ⚠️ 它们**不是装饰字段** —— `validate.py` 的 `weather_conflict` 判据靠 `type`
#    区分"户外景点"与"室内场馆"（武侯祠是博物馆=室内，青城山是风景名胜=户外）。
#    缺了它那条判据在 mock 下**永远走不到**，测试也就写不出来。

MOCK_POI_POOL: tuple[AmapPoi, ...] = (
    AmapPoi(
        poi_id="B001C07VJ2",
photos=[
            "http://store.is.autonavi.com/showpic/2a74b5da7192e4628a9bdcc368e002ac",
            "http://store.is.autonavi.com/showpic/96a6372e5faa32096847b76b5a59af7c",
            "http://store.is.autonavi.com/showpic/d60e252c0c13b260465aeba6b146c832",
        ],
        name="成都武侯祠博物馆",
        type='科教文化服务;博物馆;博物馆',
        typecode='140100',
        alias=["武侯祠", "武侯祠博物馆"],
        lng=104.047992,
        lat=30.646168,
        rating="4.8",
        open_time="08:30-18:30",
    ),
    AmapPoi(
        poi_id="B0FFFD3P2C",
photos=[
            "http://store.is.autonavi.com/showpic/d474b5c8c8e429854cf808e1445bc15e",
            "http://store.is.autonavi.com/showpic/fa2f82b05e0bdf05626cd4bb7f3ffe50",
            "http://store.is.autonavi.com/showpic/05afef84c3ef348eb593611b79a1783e",
        ],
        name="锦里古街",
        type='风景名胜;风景名胜;风景名胜',
        typecode='110200',
        alias=["锦里"],
        lng=104.049828,
        lat=30.645994,
        rating="4.8",
        open_time="24小时营业",
    ),
    AmapPoi(
        poi_id="B001C7X8QA",
photos=[
            "https://aos-comment.amap.com/B001C7X8QA/comment/content_media_external_file_100003153_1767367568289_40288788.jpg",
            "https://aos-comment.amap.com/B001C7X8QA/comment/57CB8B05_1E45_4134_9A2D_865DBEEE553B_L0_001_1179_86_1766480459732_07341334.jpg",
            "https://aos-comment.amap.com/B001C7X8QA/comment/1F5D05D1_071C_45F4_B86C_1DE32DB1151A_L0_001_1206_120_1767139271327_64300218.jpg",
        ],
        name="人民公园",
        type='风景名胜;风景名胜;国家级景点',
        typecode='110202',
        lng=104.057641,
        lat=30.656990,
        rating="4.8",
        open_time="24小时营业",
    ),
    AmapPoi(
        poi_id="B001C7YCM4",
photos=[
            "https://aos-comment.amap.com/B0KULU7RGH/comment/content_media_external_file_1000009867_ss__1749366824374_55625255.jpg",
            "https://store.is.autonavi.com/showpic/8ff1cd47593e169a8d6ddd44500a8004",
            "https://store.is.autonavi.com/showpic/d61dd46a02f41c7e0000005180958168?type=pic",
        ],
        name="宽窄巷子景区",
        type='购物服务;特色商业街;特色商业街|风景名胜;风景名胜相关;旅游景点',
        typecode='061000|110000',
        alias=["宽窄巷子"],
        lng=104.053307,
        lat=30.663869,
        rating="4.8",
        open_time="24小时营业",
    ),
    AmapPoi(
        poi_id="B001C7WE5S",
photos=[
            "https://aos-comment.amap.com/B001C7WE5S/comment/content_media_external_file_40545_1767501720604_43994986.jpg",
            "https://aos-comment.amap.com/B001C7WE5S/comment/D9A52A3A_A680_4447_9647_0C0069242BF2_L0_001_1500_200_1767303057334_10874921.jpg",
            "http://store.is.autonavi.com/showpic/f8c5d8db0b8aa5fbbd3cd63202204c26",
        ],
        name="成都大熊猫繁育研究基地",
        type='风景名胜;风景名胜;国家级景点',
        typecode='110202',
        alias=["熊猫基地", "大熊猫基地", "熊猫繁育研究基地"],
        lng=104.138176,
        lat=30.740573,
        rating="4.8",
        # ⚠️ 双时段 = 旺季/淡季两套时间，实测真值就是这样，别当脏数据
        open_time="07:30-17:00 08:00-16:30",
    ),
    AmapPoi(
        poi_id="B0FFF6X49V",
photos=[
            "http://store.is.autonavi.com/showpic/4E377E858534426097901E10EEAFCD8E",
            "http://store.is.autonavi.com/showpic/8b692655d3f68031abdae39110a0dc84",
            "https://store.is.autonavi.com/showpic/3b1a0a5246ededd30000003055250816?type=pic",
        ],
        name="成都太古里",
        type='购物服务;商场;购物中心',
        typecode='060101',
        alias=["太古里"],
        lng=104.083809,
        lat=30.653358,
        rating="4.9",
        open_time="10:00-22:00",
    ),
    AmapPoi(
        poi_id="B001C8RQLM",
photos=[
            "http://store.is.autonavi.com/showpic/023908e1ac8e27470b30bced7c4eb58c",
            "http://aos-cdn-image.amap.com/sns/ugccomment/70f19abf-88d4-4228-ad8f-bc31d638d747.jpg",
            "https://store.is.autonavi.com/showpic/90b6afd64de8ee880000005305878873?type=pic",
        ],
        name="春熙路步行街",
        type='购物服务;特色商业街;特色商业街',
        typecode='061000',
        alias=["春熙路"],
        lng=104.077774,
        lat=30.655544,
        rating="4.9",
        open_time="24小时营业",
    ),
    AmapPoi(
        poi_id="B001C06PA9",
photos=[
            "http://store.is.autonavi.com/showpic/31fab45390b5932dfa51a67dbf8548f4",
            "http://store.is.autonavi.com/showpic/9dd769964bd96b13c2990058c0bfce5c",
            "http://store.is.autonavi.com/showpic/7b311531ba597ec4464683df23df3365",
        ],
        name="都江堰景区",
        type='风景名胜;风景名胜;国家级景点',
        typecode='110202',
        alias=["都江堰"],
        lng=103.610529,
        lat=31.003363,
        rating="4.8",
        open_time="08:00-18:00 08:00-17:30",
    ),
    AmapPoi(
        poi_id="B001C06ESL",
photos=[
            "http://store.is.autonavi.com/showpic/259f92e8e83511f6ef58afd321d2a08e",
            "http://store.is.autonavi.com/showpic/653d22dcaa4c11b7b55ad375fc629dea",
            "http://store.is.autonavi.com/showpic/7fc9d943f628d5ba2b2c8379d17c8606",
        ],
        name="青城山景区",
        type='风景名胜;风景名胜;世界遗产',
        typecode='110201',
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
#  检索标签 —— mock 的「全文检索」模拟
#
#  🔴 **为什么必须有这张表**（2026-09-16 补，写图测试时发现的真遗漏）
#
#  最初的 `search_poi` 只做「名称 / 别名双向包含」匹配。跑端到端测试立刻炸：
#  模型搜「古迹」「美食」「公园」→ 一个都命中不了 → 池子空 → 整条流程失败。
#
#  这不是测试的毛病，是 **mock 保真度不够**：
#
#  | | 真实高德 | 最初的 mock |
#  |---|---|---|
#  | 匹配方式 | **全文检索**（关键词 vs 名称/分类/地址/标签） | 只比名称和别名 |
#  | 搜「古迹」 | 返回一堆景点 | **返回空** |
#  | 搜「火锅」 | 返回一堆火锅店 | 返回空 |
#
#  而 prompt 里恰恰引导模型「同一类换不同关键词各搜一遍」。所以 mock 模式
#  会**几乎必然**卡在"候选池是空的" —— 而 M1 的验收就是在 mock 模式下跑出行程。
#
#  ⚠️ 这些标签是**检索索引，不是 POI 数据字段**，两者别混：
#    · POI 数据字段（name/lng/rating/open_time…）必须是实测真值，
#      没有就留 None，**绝不编**
#    · 检索标签只决定「搜什么词能命中它」，它**不会出现在产物里**，
#      也不会被校验层读到 —— 所以它可以是人为定义的
#
#  这样切分的意义：mock 的**数据**仍然是诚实的，只是**检索行为**被放宽到
#  接近真实高德。M2 换成真接口后，这张表连同整个 mock 一起消失。
#
#  ⚠️ 池子里**没有餐饮类 POI**（实测抓的 9 个全是景点/街区/公园）。
#  所以搜「火锅」「美食」会命中锦里、宽窄巷子（那里确实有小吃），
#  但**搜不到一家真正的餐厅** —— 这是 mock 的已知空缺，不是 bug。
#  M2 用真 Key 抓一批餐饮进来才能补上。所以 M1 的行程里"吃"是缺失的，
#  验收时**不要以为这是 agent 的问题**。
# ══════════════════════════════════════════════════════════════

_MOCK_SEARCH_TAGS: dict[str, tuple[str, ...]] = {
    "B001C07VJ2": ("古迹", "历史", "博物馆", "景点", "三国", "文化", "室内"),
    "B0FFFD3P2C": ("美食", "小吃", "街区", "景点", "夜游", "免费"),
    "B001C7X8QA": ("公园", "休闲", "茶馆", "景点", "免费", "慢节奏"),
    "B001C7YCM4": ("街区", "小吃", "美食", "景点", "夜游", "古迹"),
    "B001C7WE5S": ("亲子", "动物", "景点", "自然", "必去", "户外"),
    "B0FFF6X49V": ("购物", "美食", "商业", "街区", "网红", "室内"),
    "B001C8RQLM": ("购物", "商业", "街区", "美食", "网红"),
    "B001C06PA9": ("古迹", "景点", "自然", "历史", "水利", "户外"),
    "B001C06ESL": ("自然", "山", "景点", "古迹", "道教", "户外"),
}


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
#
# 🔴 2026-09-15 校准：**"单一常量"模型是错的**。
#
# 原先用 `直线 × 1.3 ÷ 28km/h` 一套常量估所有距离。实测四条不同距离段的样本才发现，
# **路网系数和平均车速都随距离段变化**，而且变化很大：
#
# | 距离段 | 样本 | 直线 km | 驾车 km | 分钟 | 路网系数 | 均速 |
# |---|---|---|---|---|---|---|
# | 市区内 | 武侯祠→宽窄巷子 | 2.0 | 4.4 | 24 | **2.18** | **11.2** |
# | 近郊 | 人民公园→熊猫基地 | 12.1 | 20.4 | 44 | **1.69** | **28.0** |
# | 远郊 | 武侯祠→都江堰 | 57.6 | 63.9 | 75 | **1.11** | **50.9** |
# | 远郊 | 武侯祠→青城山 | 54.4 | 64.3 | 89 | **1.18** | **43.6** |
#
# 原因很直白：市区 2 公里要绕单行道、等十几个红绿灯；远郊上快速路一路 60km/h。
#
# ⚠️ **这个偏差会直接变成假阳性**：改之前 mock 把「人民公园→都江堰」算成 160 分钟，
#    而实测是 75 分钟 —— 于是 `reachable` 判据（带长辈上限 90 分钟）把一份**正常行程
#    判成硬错、白白打回两轮**。mock 的误差一旦传导到判据上，就不再是"数值不准"，
#    而是"结论错了"。
#
# 顺带一提：原来那对常量 `1.3 / 28.0` **恰好只对"近郊"这一段准**（12km 那条实测就是
# 1.689 / 28.0）。所以它不算"随便填的"，只是被当成了全局值。

_SEGMENTS: tuple[tuple[float, float, float], ...] = (
    (5.0, 2.18, 11.2),
    (30.0, 1.69, 28.0),
    (math.inf, 1.15, 47.0),
)
"""`(直线距离上界 km, 路网系数, 平均车速 km/h)` —— 按直线距离分三段取参数。

取"上界"而不是"区间"，是为了让查表逻辑只要一个 `for` 循环；
最后一段用 `inf` 兜底，所以**永远查得到**，不会有"落在所有区间之外"的分支
（那种分支在真实项目里就是"某个距离段的行为没人测过"）。

远郊那一段的系数取 1.15、速度取 47，是都江堰（1.11/50.9）和青城山（1.18/43.6）的中间值 ——
**两条样本往两边夹**，比只信一条稳。"""


def _segment_for(straight_km: float) -> tuple[float, float]:
    """按直线距离查 `(路网系数, 均速)`。"""
    for upper, factor, speed in _SEGMENTS:
        if straight_km < upper:
            return factor, speed
    return _SEGMENTS[-1][1], _SEGMENTS[-1][2]


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
            # 名称 / 别名：双向包含。搜"武侯祠"能命中"成都武侯祠博物馆"，
            # 搜全名也能命中别名。
            candidates = [poi.name, *poi.alias]
            if any(kw in c.lower() or c.lower() in kw for c in candidates):
                hits.append(poi)
                continue
            # 检索标签：模拟高德的全文检索（见 `_MOCK_SEARCH_TAGS` 的说明）。
            # ⚠️ 这里放 `continue` 而不是 `or`，是为了让"名称命中"和"标签命中"
            # 走同一条路 —— 以后要区分"精确命中"和"模糊命中"（比如排序权重）时，
            # 只需在第一个分支里加东西，不用重构条件。
            tags = _MOCK_SEARCH_TAGS.get(poi.poi_id, ())
            if any(kw in t.lower() or t.lower() in kw for t in tags):
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
        factor, speed = _segment_for(straight)
        km = straight * factor
        minutes = round(km / speed * 60)
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
