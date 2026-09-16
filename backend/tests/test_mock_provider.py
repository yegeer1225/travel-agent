"""mock 数据源的契约自检。

这批测试不是为了"证明 mock 能跑"，而是为了钉住**三件容易悄悄坏掉的事**：

1. **mock 与 real 同签名**（D23）—— mock 掉队 = 豆包写的页面接真数据时崩
2. **失败路径真的会失败** —— 搜不到就返回空，不偷偷返回"最接近的几个"
   （mock 太"聪明"会让 M1~M3 的空结果分支永远测不到）
3. **`unavailable` 而不是硬凑** —— 天气窗口外必须如实说查不到
4. **距离数值被实测钉住** —— mock 的误差会传导成**判据的结论错误**（不是"不准"而已），
   所以它和真数据一样需要断言。见「四、距离」一节。

不引 `pytest-asyncio`：M1 只有 mock provider 是异步的，为此多一个插件
就多一处要钉的版本（`requirements.txt` 里已经有三个包不能松）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

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
#  四、距离 —— 用实测样本校准（2026-09-15 重写）
# ══════════════════════════════════════════════════════════════
#
#  这一节从"检查返回值像不像距离"升级成"检查 mock 的距离**准不准**"。
#
#  起因不是洁癖，是一次假阳性：跨天判据（`validate.py` 的 `reachable`）在 mock 下
#  把「人民公园→都江堰」算成 160 分钟（实测 75 分钟），于是**把一份正常行程判成
#  硬错、白白打回两轮**。
#  → 结论：mock 的数值误差一旦传导到判据上，就不再是"数值不准"，而是"结论错了"。
#
#  ⚠️ 期望值的来源**不是**"我记得实测是多少"，而是探针落盘的 fixture
#  （`tests/fixtures/amap/distance_calibration.json`，由 `probe_amap.py` 的
#  校准采样节生成）。手写的期望值改起来太顺手，而改 fixture 必须重跑探针 ——
#  这就是"把结论绑在可重跑路径上"的意思。

_FIXTURES = Path(__file__).parent / "fixtures" / "amap"
_CALIBRATION: list[dict] = json.loads(
    (_FIXTURES / "distance_calibration.json").read_text(encoding="utf-8")
)["cases"]


def _coords(name_fragment: str) -> tuple[float, float]:
    """按名称/别名片段在池子里找坐标。找不到就报错（而不是跳过）——
    悄悄跳过会让"样本名写错了"表现为"测试通过但什么都没测"。"""
    for poi in MOCK_POI_POOL:
        if name_fragment in poi.name or name_fragment in poi.alias:
            return (poi.lng, poi.lat)
    raise AssertionError(f"mock 池里没有匹配 {name_fragment!r} 的 POI（样本名写错了？）")


def _calibration_pair(label: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """从 fixture 的 label（形如 `远郊   武侯祠→都江堰`）反解出两端坐标。"""
    _, _, route = label.partition(" ")
    origin, _, dest = route.partition("→")
    return _coords(origin.strip()), _coords(dest.strip())


def _effective_speed_kmh(d: DistanceResult) -> float:
    """用返回的两个值反推均速。输出是四舍五入过的，所以这个值只用于**比大小**。"""
    return d.km / d.drive_min * 60 if d.drive_min else 0.0


@pytest.mark.parametrize("case", _CALIBRATION, ids=[c["label"].replace(" ", "_") for c in _CALIBRATION])
def test_calc_distance_matches_measured_sample(
    provider: MockAmapProvider,
    case: dict,
) -> None:
    """mock 的驾车距离/耗时必须落在**实测样本**的容差内。

    容差不对称，因为两者性质不同：
    - 里程 ±10%：路网系数是稳定量（同一个城市短期内不变）
    - 耗时 ±20%：受实时路况影响，实测值本身就在抖（探针两次抓 `duration`
      得到 5214s / 5283s）→ 卡太紧会变成"过几天自己变红"的测试

    这个界够松到不误报，也够紧到能拦住错误模型：
    旧的"全局 1.3 / 28.0"把都江堰算成 160 分钟，**偏 +113%**，照样红。
    """
    origin, dest = _calibration_pair(case["label"])
    d = run(provider.calc_distance(origin, dest))

    assert isinstance(d, DistanceResult)
    assert d.km > d.straight_km, "驾车里程必须 > 直线距离（系数 > 1），否则是坐标系搞反了"
    assert d.drive_min > 0
    assert abs(d.straight_km - case["straight_km"]) <= 0.05, (
        f"{case['label']}：直线距离对不上，fixture={case['straight_km']} mock={d.straight_km}"
    )
    assert abs(d.km - case["km"]) / case["km"] <= 0.10, (
        f"{case['label']}：驾车里程偏差 >10%，实测 {case['km']}km mock {d.km}km"
    )
    assert abs(d.drive_min - case["minutes"]) / case["minutes"] <= 0.20, (
        f"{case['label']}：驾车耗时偏差 >20%，实测 {case['minutes']}min mock {d.drive_min}min"
    )


def test_calc_distance_same_point_is_zero(provider: MockAmapProvider) -> None:
    p = (104.047992, 30.646168)
    d = run(provider.calc_distance(p, p))
    assert d.km == 0
    assert d.drive_min == 0


def test_road_factor_decreases_while_speed_increases(provider: MockAmapProvider) -> None:
    """🔴 路网系数随距离**递减**、均速随距离**递增**。

    这一条钉住的不是某个数值，而是**模型的形状** —— 它比数值断言更难被绕过。

    实测（见 `mock.py` 的「距离」一节）：
    | 距离段 | 路网系数 | 均速 |
    |---|---|---|
    | 市区内 | 2.18 | 11.2 |
    | 近郊 | 1.69 | 28.0 |
    | 远郊 | 1.15 | 47.0 |

    物理原因很直白：市区两公里要绕单行道、等十几个红绿灯；远郊上快速路一路六十。
    **原先的"全局常量 1.3 / 28.0"在这两个方向上都画成一条平线** ——
    谁把分段表退回成一套常量，这条立刻红。
    """
    by_distance = {
        "市区内": ("武侯祠", "宽窄巷子"),
        "近郊": ("人民公园", "熊猫基地"),
        "远郊": ("武侯祠", "都江堰"),
    }
    factors: list[float] = []
    speeds: list[float] = []
    for label, (a, b) in by_distance.items():
        d = run(provider.calc_distance(_coords(a), _coords(b)))
        factors.append(d.km / d.straight_km)
        speeds.append(_effective_speed_kmh(d))
        assert d.km > d.straight_km, f"{label} 的驾车里程不该小于直线距离"

    assert factors[0] > factors[1] > factors[2], f"路网系数应递减，实得 {factors}"
    assert speeds[0] < speeds[1] < speeds[2], f"均速应递增，实得 {speeds}"


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

    ⚠️ `photos` 曾在本条里（未采集必须空）；2026-09-16 实测补采真值后
    反转到下面 `test_mock_pois_photos_are_measured`（与 typecode 同一个反转逻辑）。
    """
    assert len(MOCK_POI_POOL) == 9

    for poi in MOCK_POI_POOL:
        assert poi.cost_per_person is None, f"{poi.name} 的 cost 应为 None（人均≠门票）"


def test_mock_pois_photos_are_measured() -> None:
    """`photos` 已**补采真值**（2026-09-16，`/v3/place/text` 按 poi_id 精确匹配）：
    每个 POI 恰好 3 张、域名必须是高德系真图床 —— hero 轮播的素材来源（A39）。

    🔴 判据用**域名后缀**而不是完整域名白名单：实测图床至少有
    `store.is.autonavi.com` / `aos-comment.amap.com` / `aos-cdn-image.amap.com`
    三个并存，高德加一个 CDN 域名就让白名单误报 = 判据反过来咬自己。
    高德系图床都挂在 `.amap.com` / `.autonavi.com` 下（阿里自有域）。

    反转判据与 typecode 同款：填了值就必须挡住"编造"。
    """
    from urllib.parse import urlparse

    for poi in MOCK_POI_POOL:
        assert len(poi.photos) == 3, f"{poi.name} 应有 3 张实测照片"
        for url in poi.photos:
            host = urlparse(url).hostname or ""
            assert host.endswith((".amap.com", ".autonavi.com")), (
                f"{poi.name} 的照片域名不对（疑似编造）: {url}"
            )


def test_mock_pois_types_are_measured_not_invented() -> None:
    """`type` / `typecode` 已**补采真值**，所以这条从"必须为 None"反转成"**格式必须像真值**"。

    ⚠️ 这个反转本身是一条判据：**一旦某个字段填了值，就得有东西能挡住"编造"**。
    没有格式校验的话，别人看到 `typecode="110100"` 根本分不出它是查来的还是编的。

    （2026-09-15 采自 `probe_amap.py --collect-mock`，按 `poi_id` 精确匹配。）
    """
    measured = 0
    for poi in MOCK_POI_POOL:
        if poi.typecode is not None:
            measured += 1
            for part in poi.typecode.split("|"):
                assert part.isdigit() and len(part) == 6, (
                    f"{poi.name} 的 typecode 不像高德真值：{poi.typecode!r}（应形如 '140100'）"
                )
        if poi.type is not None:
            for segment in poi.type.split("|"):
                assert segment.count(";") == 2, (
                    f"{poi.name} 的 type 段不是「大类;中类;小类」三级结构：{segment!r}"
                )

    assert measured == len(MOCK_POI_POOL), "9 个 POI 的 typecode 应该都采到了（否则说明探针漏了某条）"


def test_describe_advertises_mock_mode(provider: MockAmapProvider) -> None:
    """启动日志里必须能一眼看出是 mock —— 否则"数据怎么不对"会查很久。"""
    text = provider.describe()
    assert "Mock" in text
    assert "POI" in text
    assert str(TODAY) in text
