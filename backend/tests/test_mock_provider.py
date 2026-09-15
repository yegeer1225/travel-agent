"""mock 数据源的契约自检。

这批测试不是为了"证明 mock 能跑"，而是为了钉住**三件容易悄悄坏掉的事**：

1. **mock 与 real 同签名**（D23）—— mock 掉队 = 豆包写的页面接真数据时崩
2. **失败路径真的会失败** —— 搜不到就返回空，不偷偷返回"最接近的几个"
   （mock 太"聪明"会让 M1~M3 的空结果分支永远测不到）
3. **`unavailable` 而不是硬凑** —— 天气窗口外必须如实说查不到

不引 `pytest-asyncio`：M1 只有 mock provider 是异步的，为此多一个插件
就多一处要钉的版本（`requirements.txt` 里已经有三个包不能松）。
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from app.providers import build_provider
from app.providers.base import AmapProvider, DistanceResult
from app.providers.mock import MOCK_CITY, MOCK_POI_POOL, MockAmapProvider
from app.schemas import WeatherStatus

# 固定"今天"，否则跑一段时间后天气断言会因为真实日期推进而失效
# —— 这类"过几天自己变红"的测试是最难查的
TODAY = date(2026, 9, 15)


def run(coro):
    """在同步测试里跑协程。"""
    return asyncio.run(coro)


@pytest.fixture()
def provider() -> MockAmapProvider:
    return MockAmapProvider(today=TODAY)


# ══════════════════════════════════════════════════════════════
#  一、同签名（D23）
# ══════════════════════════════════════════════════════════════


def test_mock_satisfies_provider_protocol(provider: MockAmapProvider) -> None:
    """mock 必须结构性满足 `AmapProvider`。

    这是 D23 的**机器化检查**：靠人记得"改 real 时同步改 mock"是不可靠的，
    靠 `runtime_checkable` Protocol 是确定的。
    """
    assert isinstance(provider, AmapProvider)
    assert provider.name == "mock"


def test_build_provider_defaults_to_mock() -> None:
    """默认（`.env` 里 `AMAP_PROVIDER=mock`）造出来的就是 mock。"""
    assert isinstance(build_provider(), MockAmapProvider)


# ══════════════════════════════════════════════════════════════
#  二、搜索：命中、不命中、边界
# ══════════════════════════════════════════════════════════════


def test_search_by_full_name(provider: MockAmapProvider) -> None:
    hits = run(provider.search_poi("成都武侯祠博物馆", city=MOCK_CITY))
    assert [p.poi_id for p in hits] == ["B001C07VJ2"]


def test_search_by_alias(provider: MockAmapProvider) -> None:
    """搜简称要能命中全名 —— 校验层的模糊匹配就靠这个（风险 3）。"""
    hits = run(provider.search_poi("熊猫基地", city=MOCK_CITY))
    assert len(hits) == 1
    assert hits[0].name == "成都大熊猫繁育研究基地"


def test_search_is_bidirectionally_contained(provider: MockAmapProvider) -> None:
    """名字和别名**双向**包含都算命中。

    只做单向（keyword ⊂ name）会漏掉"用户搜了完整名称、
    但池子里存的是景区全称带后缀"这类情况。
    """
    hits = run(provider.search_poi("宽窄巷子", city=MOCK_CITY))
    assert hits and hits[0].poi_id == "B001C7YCM4"


def test_search_returns_empty_not_error(provider: MockAmapProvider) -> None:
    """🔴 搜不到 = 返回空列表，**不是抛异常**。

    契约要求调用方把"空"当正常业务结果（校验层里它是软提示不是硬错）。
    如果 mock 在这里报错，M1~M3 的空结果分支就永远跑不到。
    """
    assert run(provider.search_poi("火锅", city=MOCK_CITY)) == []
    assert run(provider.search_poi("不存在的地方xyz", city=MOCK_CITY)) == []


def test_search_blank_keyword_returns_empty(provider: MockAmapProvider) -> None:
    """空关键词返回空 —— 不发请求。

    ⚠️ 真正的守卫在 `api.md` 的 `keyword_required`（前端不该发这个请求）。
    这一条是**后端兜底**。
    """
    assert run(provider.search_poi("", city=MOCK_CITY)) == []
    assert run(provider.search_poi("   ", city=MOCK_CITY)) == []


def test_search_other_city_returns_empty(provider: MockAmapProvider) -> None:
    """mock 池里只有成都 —— 查别的城市必须返回空，不能张冠李戴。"""
    assert run(provider.search_poi("武侯祠", city="北京")) == []


def test_search_respects_limit(provider: MockAmapProvider) -> None:
    hits = run(provider.search_poi("成都", city=MOCK_CITY, limit=2))
    assert len(hits) <= 2


def test_poi_pool_ids_are_unique() -> None:
    """池子里不能有重复 `poi_id` —— 封闭世界校验以它为锚点，重复会让判定失效。"""
    ids = [p.poi_id for p in MOCK_POI_POOL]
    assert len(ids) == len(set(ids)) == 9


# ══════════════════════════════════════════════════════════════
#  三、天气：4 天窗口（真实约束，mock 必须同行为）
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("offset", [0, 1, 2, 3])
def test_weather_within_window_is_ok(provider: MockAmapProvider, offset: int) -> None:
    """窗口内（今天 ~ 今天+3，共 4 天）必须给出可用预报。"""
    w = run(provider.get_weather(MOCK_CITY, TODAY + timedelta(days=offset)))
    assert w.status == WeatherStatus.OK
    assert w.day_weather and w.day_temp is not None
    assert w.report_time is not None


def test_weather_outside_window_is_unavailable(provider: MockAmapProvider) -> None:
    """🔴 窗口外返回 `unavailable` + `note`，**不硬凑一个预报**。

    这条对应契约里最容易被写错的一处：`unavailable` 不是"校验失败"，
    是"这个数据拿不到" → 界面必须显示"无法判定"，**不能显示"通过"**。
    """
    w = run(provider.get_weather(MOCK_CITY, TODAY + timedelta(days=5)))
    assert w.status == WeatherStatus.UNAVAILABLE
    assert w.note and "预报窗口" in w.note
    assert w.day_weather is None


def test_weather_past_date_is_unavailable(provider: MockAmapProvider) -> None:
    w = run(provider.get_weather(MOCK_CITY, TODAY - timedelta(days=1)))
    assert w.status == WeatherStatus.UNAVAILABLE
    assert w.note is not None


def test_weather_other_city_is_unavailable(provider: MockAmapProvider) -> None:
    w = run(provider.get_weather("北京", TODAY))
    assert w.status == WeatherStatus.UNAVAILABLE
    assert w.note is not None


# ══════════════════════════════════════════════════════════════
#  四、距离
# ══════════════════════════════════════════════════════════════


def test_calc_distance_returns_road_longer_than_straight(
    provider: MockAmapProvider,
) -> None:
    """驾车里程必须 ≥ 直线距离（绕行系数 > 1）。

    比值异常（比如 < 1）说明坐标系搞反了或系数写错 —— 用 `straight_km`
    交叉验证是**刻意保留的第二个值**，就是为了让这种错能被断言出来。
    """
    wuhou = (104.047992, 30.646168)
    panda = (104.138176, 30.740573)
    d = run(provider.calc_distance(wuhou, panda))

    assert isinstance(d, DistanceResult)
    assert d.km > d.straight_km
    assert d.drive_min > 0
    assert 8.0 < d.km < 20.0, f"武侯祠→熊猫基地 实测驾车约 15km，mock 给出 {d.km}"


def test_calc_distance_same_point_is_zero(provider: MockAmapProvider) -> None:
    p = (104.047992, 30.646168)
    d = run(provider.calc_distance(p, p))
    assert d.km == 0
    assert d.drive_min == 0


# ══════════════════════════════════════════════════════════════
#  五、三态的真实来源
# ══════════════════════════════════════════════════════════════


def test_qingchengshan_has_no_open_time(provider: MockAmapProvider) -> None:
    """🔴 青城山的 `open_time` 必须是 `None`。

    这不是"mock 缺数据"，是**实测事实**：高德确实没返回这个 POI 的开放时间。
    校验层的 `open_today` 判 `unknown` 就靠它 —— 如果哪天有人"顺手补上"一个
    看起来合理的时间，三态里 `unknown` 这条路径就再也没数据能触发了。
    """
    hits = run(provider.search_poi("青城山", city=MOCK_CITY))
    assert len(hits) == 1
    assert hits[0].open_time is None


def test_double_schedule_open_time_is_preserved(provider: MockAmapProvider) -> None:
    """双时段（旺季/淡季）的 `open_time` 要**原样保留**，不要自作聪明取第一个。

    实测真值形如 `'07:30-17:00 08:00-16:30'`。解析它是校验层的事，
    provider 的职责是**不丢信息** —— 在这里切开，校验层就再也拿不到淡季时间了。
    """
    hits = run(provider.search_poi("熊猫基地", city=MOCK_CITY))
    assert hits[0].open_time == "07:30-17:00 08:00-16:30"


def test_mock_pois_declare_unknown_fields_as_none() -> None:
    """未实测采集的字段一律 `None` / 空 —— **mock 不许编造**。

    假 `typecode` 会让"景点页类型过滤"在 mock 下看起来能用、接真数据才发现对不上；
    给 `None` 则让那段逻辑现在就走"无数据"分支，问题当场暴露。
    """
    assert len(MOCK_POI_POOL) == 9

    for poi in MOCK_POI_POOL:
        assert poi.typecode is None, f"{poi.name} 的 typecode 不该有值（未采集）"
        assert poi.photos == [], f"{poi.name} 的 photos 不该有值（未采集）"
        assert poi.cost_per_person is None, f"{poi.name} 的 cost 应为 None（人均≠门票）"


def test_describe_advertises_mock_mode(provider: MockAmapProvider) -> None:
    """启动日志里必须能一眼看出是 mock —— 否则"数据怎么不对"会查很久。"""
    text = provider.describe()
    assert "Mock" in text
    assert "POI" in text
    assert str(TODAY) in text
