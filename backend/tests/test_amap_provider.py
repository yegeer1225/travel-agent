"""高德真实 provider 的测试。

**全程不打网络**，两个理由：
① 单元测试不该消耗配额，也不该因为高德抖动而随机变红
② 但**解析逻辑必须吃到真实样本** —— 所以样本是 `scripts/probe_amap.py` 采下来的**原始响应**
   （`tests/fixtures/amap/`），不是手写的"理想 JSON"。
   手写的理想 JSON 测不出「`alias` 有时是字符串有时是数组」这种问题 —— 而那正是真坑。

网络行为（重试/限流/退避）用 `httpx.MockTransport` 打桩：
响应体是**真实抓下来的错误样本**，不是编的。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from app.providers.amap import AmapError, AmapHttpProvider, _num, _text, parse_poi
from app.providers.base import AmapProvider
from app.schemas import WeatherStatus

FIXTURES = Path(__file__).parent / "fixtures" / "amap"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def run(coro):
    return asyncio.run(coro)


def first_poi(name: str) -> dict[str, Any]:
    return load(name)["pois"][0]


# ══════════════════════════════════════════════════════════════
#  打桩：按顺序弹出预设响应
# ══════════════════════════════════════════════════════════════


class FakeAmap:
    """一个"照着剧本回话"的高德。剧本里可以放 dict（正常响应）或 Exception（网络炸）。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.script:
            raise AssertionError(f"剧本用完了，但又收到了请求：{request.url}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item)

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def params(self, index: int = 0) -> dict[str, str]:
        return dict(self.requests[index].url.params)


def make_provider(fake: FakeAmap, **kwargs: Any) -> AmapHttpProvider:
    """造一个 provider，网络被替换成剧本。

    ⚠️ `min_interval_s=0` + `retry_backoff_s=(0,0,0)` 是**故意的**：
       默认的 0.45s 限速和 1/2/4s 退避会让测试慢到没人愿意跑。
       限速与退避本身另有专门的测试（用真实的小间隔验）。
    """
    kwargs.setdefault("min_interval_s", 0.0)
    kwargs.setdefault("retry_backoff_s", (0.0, 0.0, 0.0))
    provider = AmapHttpProvider("test-key", **kwargs)
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), timeout=5)
    return provider


def ok(payload: dict[str, Any]) -> dict[str, Any]:
    return payload


def rate_limited() -> dict[str, Any]:
    """实测原样抓下来的限流响应（`scripts/probe_amap.py` 第 ⑧ 步）。"""
    return {"status": "0", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT", "infocode": "10021"}


def bad_key() -> dict[str, Any]:
    return {"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"}


def daily_quota() -> dict[str, Any]:
    return {"status": "0", "info": "USER_DAILY_QUERY_OVER_LIMIT", "infocode": "10003"}


# ══════════════════════════════════════════════════════════════
#  ① 字段清洗 —— 高德的类型不稳定全在这里挡住
# ══════════════════════════════════════════════════════════════


def test_text_collapses_all_four_ways_of_saying_nothing():
    """高德说"这个字段没有"有四种写法，**都必须收敛成 None**。

    最阴的是 `'[]'` —— 字符串形态的空数组。它 truthy、长度 2、`if not value` 拦不住，
    一路流到界面上就会显示成 `[]`。
    """
    assert _text(None) is None
    assert _text("") is None
    assert _text("   ") is None
    assert _text([]) is None
    assert _text("[]") is None
    assert _text("{}") is None
    assert _text("null") is None
    assert _text("武侯祠大街231号") == "武侯祠大街231号"


def test_num_keeps_zero_distinct_from_none():
    """`cost=0`（免费）和 `cost=None`（没数据）是两件事 —— 不能把空值兜底成 0。"""
    assert _num("73.00") == 73.0
    assert _num("0") == 0.0
    assert _num("0.00") == 0.0
    assert _num([]) is None
    assert _num("[]") is None
    assert _num(None) is None
    assert _num("不是数字") is None


# ══════════════════════════════════════════════════════════════
#  ② 解析真实样本
# ══════════════════════════════════════════════════════════════


def test_parse_real_wuhouci_sample():
    """武侯祠 —— 实测原始响应。`alias` 在这里是**字符串** `'武侯祠|武侯祠景区'`。"""
    poi = first_poi("place_text_wuhouci.json")
    parsed = parse_poi(poi)

    assert parsed is not None
    assert parsed.poi_id == "B001C07VJ2"
    assert parsed.name == "成都武侯祠博物馆"
    assert parsed.alias == ["武侯祠", "武侯祠景区"], "`|` 分隔的字符串必须拆成 list"
    assert (parsed.lng, parsed.lat) == (104.047992, 30.646168)
    assert parsed.rating == "4.8"
    assert parsed.open_time == "08:30-18:30"
    assert "最晚进入" in (parsed.open_time_detail or ""), "`opentime2` 是比 open_time 更详细的信息"
    assert parsed.address == "武侯祠大街231号"
    assert parsed.adname == "武侯区"
    assert parsed.tel == "028-85535951"
    assert len(parsed.photos) == 3
    assert all(p.startswith("http") for p in parsed.photos)
    # 景点类 cost 实测是 `[]` → 必须收敛成 None，**不是 0**
    assert parsed.cost_per_person is None


def test_parse_real_food_sample_has_cost():
    """餐饮 —— `cost` 唯一有值的类别（实测：酒店/地铁/景点全是 `[]`）。"""
    parsed = parse_poi(first_poi("place_text_food.json"))
    assert parsed is not None
    assert parsed.cost_per_person == 73.0
    assert parsed.open_time == "11:00-02:00"
    assert parsed.alias == [], "餐饮的 alias 实测是空数组 —— 和景点不是一种形态"


def test_parse_every_category_sample():
    """分类别抽样出的 5 条全部能解析，且"没有的字段"是 None 不是脏值。

    这一步是**防止只测一个类别就下结论**（最容易漏的一步）。
    """
    samples = load("poi_by_category_samples.json")["pois"]
    assert len(samples) == 5

    parsed = [parse_poi(s) for s in samples]
    assert all(p is not None for p in parsed), "五条样本都该能解析"

    # ⚠️ 别按名字匹配 —— "POSHPACKER…(成都太古里春熙路**地铁站**店)" 是个酒店。
    #    按 `type` 判类别才不会误伤（这是真实样本里立刻踩到的坑）。
    metro = next(p for p in parsed if "交通设施服务" in (p.type or ""))
    assert metro.cost_per_person is None
    assert metro.rating is None, "地铁站实测 rating 是 []"
    assert metro.open_time is None
    assert metro.alias == [], "地铁站实测没有别名"

    hotel = next(p for p in parsed if "住宿服务" in (p.type or ""))
    assert hotel.rating == "4.6", "酒店**有** rating"
    assert hotel.cost_per_person is None, "酒店的 `cost` 是 []，人均在 `lowest_price` 里（不在契约内）"


def test_parse_skips_poi_without_coordinates():
    """没有坐标的 POI 直接跳过 —— 算不出距离的站点对行程没用。"""
    raw = first_poi("place_text_wuhouci.json")
    raw["location"] = []
    assert parse_poi(raw) is None
    raw["location"] = ""
    assert parse_poi(raw) is None
    raw["location"] = "104.047992"  # 只有一半
    assert parse_poi(raw) is None
    raw["location"] = "abc,def"
    assert parse_poi(raw) is None


def test_parse_skips_poi_without_id_or_name():
    for field in ("id", "name"):
        raw = first_poi("place_text_wuhouci.json")
        raw[field] = []
        assert parse_poi(raw) is None, f"缺 {field} 就该跳过"


def test_parse_survives_missing_biz_ext():
    """`biz_ext` 整个缺失的情况（地铁站实测只有 4 个键，极端情况可能没有）。"""
    raw = first_poi("place_text_wuhouci.json")
    raw["biz_ext"] = []
    parsed = parse_poi(raw)
    assert parsed is not None
    assert parsed.rating is None
    assert parsed.cost_per_person is None
    assert parsed.open_time is None


# ══════════════════════════════════════════════════════════════
#  ③ search_poi
# ══════════════════════════════════════════════════════════════


def test_search_poi_sends_required_params():
    """`extensions=all` 是硬要求 —— 不加就没有 `biz_ext`（评分/营业时间全丢）。"""
    fake = FakeAmap([load("place_text_wuhouci.json")])
    provider = make_provider(fake)

    pois = run(provider.search_poi("武侯祠", city="成都", limit=5))

    assert len(pois) == 3, "样本里 offset=3，应该全解析出来"
    params = fake.params()
    assert params["extensions"] == "all"
    assert params["keywords"] == "武侯祠"
    assert params["city"] == "成都"
    assert params["offset"] == "5"
    assert params["key"] == "test-key"


def test_search_poi_without_city_omits_param():
    fake = FakeAmap([load("place_text_wuhouci.json")])
    provider = make_provider(fake)
    run(provider.search_poi("武侯祠"))
    assert "city" not in fake.params()


def test_search_poi_empty_keyword_does_not_call_api():
    fake = FakeAmap([])  # 剧本是空的 —— 一旦发请求就会 AssertionError
    provider = make_provider(fake)
    assert run(provider.search_poi("   ")) == []
    assert fake.requests == []


def test_search_poi_raises_when_every_poi_unparsable():
    """🔴 有条目但一条都解析不出来 = **高德改了字段格式**，不是"搜不到"。

    如果这里静默返回 `[]`，上游会理解成"这个关键词没有结果"，
    于是"所有景点都不存在" → 校验层把整份行程判为无效 → 白重跑。
    数据库里只留一个成功响应，字段全被改名的场景在真实世界里就是一次静默的数据事故。
    """
    broken = load("place_text_wuhouci.json")
    broken["pois"] = [{"xxx": "yyy"}, {"zzz": 1}]
    fake = FakeAmap([broken])
    provider = make_provider(fake)

    with pytest.raises(AmapError) as exc:
        run(provider.search_poi("武侯祠"))
    assert "解析失败" in str(exc.value)
    assert exc.value.infocode == "PARSE"


def test_search_poi_genuine_empty_result_is_not_an_error():
    """**真的**空结果（`pois: []`）是正常业务结果，不能抛。

    实测高德几乎不返回空，但接口契约允许 —— 所以这条路径必须存在且安静。
    """
    fake = FakeAmap([{"status": "1", "info": "OK", "infocode": "10000", "pois": []}])
    provider = make_provider(fake)
    assert run(provider.search_poi("什么都没有")) == []


def test_search_poi_partial_parse_failure_keeps_good_records():
    """一条脏记录不该废掉整次搜索（M1 在抽取层踩过的同款坑）。"""
    mixed = load("place_text_wuhouci.json")
    mixed["pois"] = [mixed["pois"][0], {"id": "X", "name": "无坐标景点", "location": []}]
    fake = FakeAmap([mixed])
    provider = make_provider(fake)
    pois = run(provider.search_poi("武侯祠"))
    assert len(pois) == 1
    assert pois[0].poi_id == "B001C07VJ2"


# ══════════════════════════════════════════════════════════════
#  ④ 限流与重试 —— 本文件最重要的一组
# ══════════════════════════════════════════════════════════════


def test_rate_limit_is_retried_not_swallowed():
    """★ 核心回归：被限流**绝不能**变成"搜不到"。

    实测复现过两次：超 QPS 时 HTTP 仍是 200，只是 `status=0` + `infocode=10021`。
    如果代码把非 1 的 status 都当"空结果"，就会出现：
      被限流 → 返回 [] → 上游判定"这个景点搜不到" → 校验判 hard error → 白重排整份行程。
    这个 bug 的可怕之处在于**它不报错**，只是偶发地给出错误结论。
    """
    fake = FakeAmap([rate_limited(), load("place_text_wuhouci.json")])
    provider = make_provider(fake)

    pois = run(provider.search_poi("武侯祠"))

    assert len(pois) == 3, "限流后重试应该拿到真结果"
    assert len(fake.requests) == 2, "第一次限流 + 第二次成功"


def test_rate_limit_exhausts_retries_then_raises():
    """重试打光仍被限流 → **抛异常**，不是返回空。"""
    fake = FakeAmap([rate_limited(), rate_limited(), rate_limited(), rate_limited()])
    provider = make_provider(fake)

    with pytest.raises(AmapError) as exc:
        run(provider.search_poi("武侯祠"))
    assert exc.value.infocode == "10021"
    assert len(fake.requests) == 4, "1 次原始 + 3 次重试"


def test_invalid_key_is_not_retried():
    """Key 错了重试没有意义 —— 必须**一次就抛**，别浪费 7 秒退避。

    （实测排除：`10001` 无法区分"Key 错"和"平台不匹配"，但两种都不该重试。）
    """
    fake = FakeAmap([bad_key()])
    provider = make_provider(fake)

    with pytest.raises(AmapError) as exc:
        run(provider.search_poi("武侯祠"))
    assert len(fake.requests) == 1
    assert "INVALID_USER_KEY" in str(exc.value)
    assert "Key 不对" in str(exc.value), "报错要能让人看懂，不是甩一个 infocode"


def test_daily_quota_error_explains_itself():
    """日配额用完 ≠ QPS 超限 —— 重试无用，且必须说清"不是代码问题"。"""
    fake = FakeAmap([daily_quota()])
    provider = make_provider(fake)

    with pytest.raises(AmapError) as exc:
        run(provider.search_poi("武侯祠"))
    assert len(fake.requests) == 1, "日配额不该重试"
    assert "日配额" in str(exc.value)


def test_transport_error_is_retried():
    """网络抖动（GET 幂等）可以重试。"""
    fake = FakeAmap([httpx.ConnectTimeout("超时"), load("place_text_wuhouci.json")])
    provider = make_provider(fake)
    pois = run(provider.search_poi("武侯祠"))
    assert len(pois) == 3
    assert len(fake.requests) == 2


def test_transport_error_exhausted_raises_amap_error():
    fake = FakeAmap([httpx.ConnectTimeout("超时")] * 5)
    provider = make_provider(fake)
    with pytest.raises(AmapError) as exc:
        run(provider.search_poi("武侯祠"))
    assert exc.value.infocode == "TRANSPORT"


def test_throttle_enforces_min_interval():
    """出站限速真的在等 —— 不然探针里复现的限流会在生产里天天撞。"""
    import time as _time

    fake = FakeAmap([load("place_text_wuhouci.json"), load("place_text_wuhouci.json")])
    provider = make_provider(fake, min_interval_s=0.08)

    async def two_calls():
        started = _time.monotonic()
        await provider.search_poi("武侯祠")
        await provider.search_poi("武侯祠")
        return _time.monotonic() - started

    assert run(two_calls()) >= 0.08


# ══════════════════════════════════════════════════════════════
#  ⑤ 天气
# ══════════════════════════════════════════════════════════════


def test_get_weather_returns_matching_day():
    fake = FakeAmap([load("geocode_chengdu.json"), load("weather_chengdu.json")])
    provider = make_provider(fake)

    weather = run(provider.get_weather("成都市", date(2026, 9, 16)))

    assert weather.status is WeatherStatus.OK
    assert weather.day_weather == "阴"
    assert weather.night_weather == "阵雨"
    assert weather.day_temp == 23
    assert weather.night_temp == 17
    # ⚠️ 实测高德原样返回 `'北'` / `'1-3'`，**不带"风"字也不带"级"字**。
    #    展示层的拼接（"北风 1-3 级"）是前端的事，数据层不预先化妆。
    assert weather.day_wind == "北"
    assert weather.day_power == "1-3"
    assert weather.report_time == "2026-09-15 21:36:30"


def test_get_weather_out_of_window_says_why():
    """窗口外**不报错**，返回 unavailable + 一句人话（契约要求）。"""
    fake = FakeAmap([load("geocode_chengdu.json"), load("weather_chengdu.json")])
    provider = make_provider(fake)

    weather = run(provider.get_weather("成都市", date(2026, 9, 25)))

    assert weather.status is WeatherStatus.UNAVAILABLE
    assert "4 天" in (weather.note or ""), "要说清窗口是几天"
    assert "2026-09-18" in (weather.note or ""), "要给出窗口的最后一天，用户才知道该改到哪天"


def test_weather_window_is_four_days_not_three():
    """⭐ 官方文档写 3 天，**实测 4 天**。这条断言是把实测结论钉住。

    （探针第 ④ 步的输出就是证据；如果哪天高德改回 3 天，这条会红，
      提醒我们回去改 `技术方案.md` 和 `api.md`。）
    """
    casts = load("weather_chengdu.json")["forecasts"][0]["casts"]
    assert len(casts) == 4
    assert casts[0]["date"] == "2026-09-15"
    assert casts[-1]["date"] == "2026-09-18"


def test_adcode_is_cached_across_calls():
    """同一个城市查多次天气，地理编码只该发一次。"""
    fake = FakeAmap([load("geocode_chengdu.json"), load("weather_chengdu.json"), load("weather_chengdu.json")])
    provider = make_provider(fake)

    run(provider.get_weather("成都市", date(2026, 9, 16)))
    run(provider.get_weather("成都市", date(2026, 9, 17)))

    assert fake.paths.count("/v3/geocode/geo") == 1
    assert fake.paths.count("/v3/weather/weatherInfo") == 2


def test_adcode_cache_does_not_remember_failures():
    """地理编码失败**不进缓存** —— 否则"先打错字、再改对"会永远拿不到数据。"""
    fake = FakeAmap([
        {"status": "1", "info": "OK", "infocode": "10000", "geocodes": []},
        load("geocode_chengdu.json"),
        load("weather_chengdu.json"),
    ])
    provider = make_provider(fake)

    first = run(provider.get_weather("不存在的地方", date(2026, 9, 16)))
    assert first.status is WeatherStatus.UNAVAILABLE
    assert "城市编码" in (first.note or "")

    second = run(provider.get_weather("不存在的地方", date(2026, 9, 16)))
    assert second.status is WeatherStatus.OK, "第二次要重新查，不能被上次的失败钉住"


def test_get_weather_without_city():
    fake = FakeAmap([])
    provider = make_provider(fake)
    weather = run(provider.get_weather("", date(2026, 9, 16)))
    assert weather.status is WeatherStatus.UNAVAILABLE
    assert fake.requests == []


def test_get_weather_with_empty_forecasts():
    fake = FakeAmap([load("geocode_chengdu.json"), {"status": "1", "info": "OK", "infocode": "10000", "forecasts": []}])
    provider = make_provider(fake)
    weather = run(provider.get_weather("成都市", date(2026, 9, 16)))
    assert weather.status is WeatherStatus.UNAVAILABLE


# ══════════════════════════════════════════════════════════════
#  ⑥ 测距
# ══════════════════════════════════════════════════════════════

WUHOU = (104.047992, 30.646168)
QINGCHENG = (103.563817, 30.904400)


def test_calc_distance_from_real_sample():
    """⭐ 实测 `/v3/distance` **同时给 distance 和 duration** —— 不用再调路径规划接口。"""
    fake = FakeAmap([load("distance_type1_wuhou_qingcheng.json")])
    provider = make_provider(fake)

    result = run(provider.calc_distance(WUHOU, QINGCHENG))

    assert result.km == 64.35, "原文 64346 米"
    assert result.drive_min == 88, "原文 5283 秒 → 88 分钟"
    assert fake.params()["type"] == "1", "必须是驾车不是直线"


def test_straight_km_is_computed_locally():
    """直线距离本地算 —— 省一次请求就少一次撞限流的机会。

    断言"驾车 > 直线"这个物理关系（路网绕行必然更长），而不是断言某个具体数字：
    具体数字依赖坐标精度，关系才是不变量。
    """
    fake = FakeAmap([load("distance_type1_wuhou_qingcheng.json")])
    provider = make_provider(fake)
    result = run(provider.calc_distance(WUHOU, QINGCHENG))

    assert 54.0 < result.straight_km < 55.0, "实测 type=0 给 54504 米，本地算应该同量级"
    assert result.km > result.straight_km, "驾车距离必须大于直线距离"
    assert len(fake.requests) == 1, "直线距离不该额外发请求"


def test_calc_distance_zero_duration_falls_back_to_estimate():
    """`duration=0` 时不能原样返回 `drive_min=0`。

    `0 分钟`会被"当天车程是否过长"的判据读成"坐车不用时间"，比一个量级正确的估算危险得多。
    （实测 `type=1` 恒有 duration，这个分支是防高德改行为的兜底。）
    """
    payload = {"status": "1", "info": "OK", "infocode": "10000",
               "results": [{"origin_id": "1", "dest_id": "1", "distance": "64346", "duration": "0"}]}
    fake = FakeAmap([payload])
    provider = make_provider(fake)
    result = run(provider.calc_distance(WUHOU, QINGCHENG))

    assert result.drive_min > 0, "绝不能给 0"
    assert 100 < result.drive_min < 150, "64km 按 30km/h 估 ≈128 分钟"


def test_calc_distance_missing_result_raises():
    for payload in (
        {"status": "1", "info": "OK", "infocode": "10000", "results": []},
        {"status": "1", "info": "OK", "infocode": "10000", "results": [{"distance": []}]},
    ):
        fake = FakeAmap([payload])
        provider = make_provider(fake)
        with pytest.raises(AmapError):
            run(provider.calc_distance(WUHOU, QINGCHENG))


# ══════════════════════════════════════════════════════════════
#  ⑦ 协议一致性
# ══════════════════════════════════════════════════════════════


def test_real_provider_satisfies_protocol():
    """结构化检查 —— mock 与 real 必须同签名（D23）。"""
    provider = AmapHttpProvider("k")
    assert isinstance(provider, AmapProvider)
    assert provider.name == "real"


def test_mock_and_real_have_same_public_surface():
    """两个实现的**参数名必须逐个一致**。

    契约冻结的意义就在这里：M2 换真数据源，`graph/` 和 `tools/` 一行都不用改。
    如果哪天有人在 real 上多加一个参数（比如 `types`），这条会红 —— 那是**好事**，
    因为"多一个参数"意味着 mock 也要跟着加，否则两边行为会静默分叉。
    """
    import inspect

    from app.providers.mock import MockAmapProvider

    for method in ("search_poi", "get_weather", "calc_distance"):
        mock_sig = inspect.signature(getattr(MockAmapProvider, method))
        real_sig = inspect.signature(getattr(AmapHttpProvider, method))
        assert list(mock_sig.parameters) == list(real_sig.parameters), (
            f"{method} 的签名不一致：mock={list(mock_sig.parameters)} real={list(real_sig.parameters)}"
        )


def test_provider_requires_key():
    with pytest.raises(ValueError):
        AmapHttpProvider("")


def test_factory_rejects_real_without_key(monkeypatch):
    """`AMAP_PROVIDER=real` 但没有 Key → 明确报错，不是悄悄用 mock。"""
    from dataclasses import replace

    from app.providers import build_provider

    fake_settings = replace(_settings(), amap_provider="real", amap_webservice_key="")
    with pytest.raises(RuntimeError) as exc:
        build_provider(fake_settings)
    assert "AMAP_WEBSERVICE_KEY" in str(exc.value)


def test_factory_builds_real_provider():
    from dataclasses import replace

    from app.providers import build_provider

    provider = build_provider(replace(_settings(), amap_provider="real", amap_webservice_key="k" * 32))
    assert isinstance(provider, AmapHttpProvider)
    assert provider.name == "real"


def _settings():
    from app.config import get_settings

    return get_settings()
