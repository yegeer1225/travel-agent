"""工具层自检。

这批测试盯的是**工具的描述与返回文本本身**，不只是"函数能跑"。
因为在 agent 项目里，工具的 docstring 和返回文案**就是 prompt** ——
它们直接决定模型会不会编造。所以要断言到文案级（"必须包含『不要……』"）。

重点三条：
1. **工具就是 3 个**（A7）—— 加工具是架构决定，不该在重构里悄悄发生
2. **搜到的必须进池子** —— 否则封闭世界校验永远拿不到判据
3. **拿不到数据时必须明说"不要编造"** —— 提示写含糊，模型就会开始圆
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.providers.mock import MOCK_CITY, MockAmapProvider
from app.schemas import AmapPoi
from app.tools import build_amap_tools, current_pool, poi_pool_scope
from app.tools.amap_tools import MAX_CANDIDATES, SEARCH_FETCH, _search_center
from app.utils import haversine_km

TODAY = date(2026, 9, 15)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def provider() -> MockAmapProvider:
    return MockAmapProvider(today=TODAY)


@pytest.fixture()
def tools(provider: MockAmapProvider) -> dict[str, object]:
    return {t.name: t for t in build_amap_tools(provider, default_city=MOCK_CITY)}


# ══════════════════════════════════════════════════════════════
#  一、工具集合本身
# ══════════════════════════════════════════════════════════════


def test_exactly_three_tools(tools: dict[str, object]) -> None:
    """**A7 的机器化检查**：工具就 3 个。

    加工具是架构决定（会让模型的选择空间变大、prompt 变长），
    不该在某次重构里顺手发生。要加就先改这里 —— 那是一次有意识的动作。
    """
    assert set(tools) == {"search_poi", "get_weather", "calc_distance"}


def test_every_tool_has_a_description(tools: dict[str, object]) -> None:
    """每个工具的 docstring 都会进模型上下文。空的 = 模型瞎猜什么时候用。"""
    for name, t in tools.items():
        assert t.description.strip(), f"{name} 缺少描述"  # type: ignore[attr-defined]


def test_constraints_are_stated_in_tool_descriptions(tools: dict[str, object]) -> None:
    """约束必须写在**工具描述**里，而不是只写在 system prompt 里。

    "别编造"这类约束放在工具描述里，模型在**决定要不要调它**的那一刻就会读到。
    """
    assert "不要" in tools["search_poi"].description  # type: ignore[attr-defined]
    assert "不要" in tools["get_weather"].description  # type: ignore[attr-defined]
    assert "不要" in tools["calc_distance"].description  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════
#  二、search_poi
# ══════════════════════════════════════════════════════════════


def test_search_returns_id_for_model(tools: dict[str, object]) -> None:
    """返回文本里必须带 `poi_id` —— 模型后续引用地点全靠它。"""

    async def scene() -> str:
        with poi_pool_scope():
            return await tools["search_poi"].ainvoke({"keyword": "武侯祠"})  # type: ignore[attr-defined]

    text = run(scene())
    assert "B001C07VJ2" in text
    assert "成都武侯祠博物馆" in text
    assert "104.047992" in text


def test_search_records_into_pool(tools: dict[str, object]) -> None:
    """🔴 搜到的必须进池子 —— 这是封闭世界校验唯一的数据来源。"""

    async def scene() -> tuple[str, int, str | None]:
        with poi_pool_scope() as pool:
            text = await tools["search_poi"].ainvoke({"keyword": "武侯祠"})  # type: ignore[attr-defined]
            poi = pool.get("B001C07VJ2")
            return text, len(pool), poi.name if poi else None

    text, size, name = run(scene())
    assert size == 1
    assert name == "成都武侯祠博物馆"


def test_search_works_without_active_pool(tools: dict[str, object]) -> None:
    """没有池子时工具**不该崩** —— 单独调试工具是常见操作。

    但注意：这是"静默不记录"，校验层必须靠 `current_pool() is None`
    自己发现"忘开 scope"，而不是以为"模型一个 POI 都没引用"。
    """
    assert current_pool() is None
    text = run(tools["search_poi"].ainvoke({"keyword": "武侯祠"}))  # type: ignore[attr-defined]
    assert "B001C07VJ2" in text


def test_search_no_match_tells_model_not_to_fabricate(tools: dict[str, object]) -> None:
    """搜不到时，文案必须**明确禁止编造**。只说"没有结果"，模型就会自己圆一个。"""

    async def scene() -> str:
        with poi_pool_scope():
            return await tools["search_poi"].ainvoke({"keyword": "火锅"})  # type: ignore[attr-defined]

    text = run(scene())
    assert "没有找到" in text
    assert "不要凭记忆" in text
    assert "编" in text


def test_search_uses_default_city_when_omitted(provider: MockAmapProvider) -> None:
    """不传 city 时用本次行程的目的地，而不是空字符串去搜全国。"""
    t = {x.name: x for x in build_amap_tools(provider, default_city=MOCK_CITY)}
    text = run(t["search_poi"].ainvoke({"keyword": "武侯祠"}))  # type: ignore[attr-defined]
    assert "B001C07VJ2" in text


# ══════════════════════════════════════════════════════════════
#  二之二、search_poi 的「多取 → 重排 → 截断」三段式
#
#  ⚠️ 这一节**故意不用 mock provider**：mock 池只有 9 个 POI，
#     而且它们本来就没有"噪音排在前面"这种形态，验不出重排。
#     这里用一个按剧本回话的桩，把"高德会把车站/政府排在前排"这件事
#     做成**可控的输入**。
# ══════════════════════════════════════════════════════════════


def make_poi(poi_id: str, name: str, type_str: str | None) -> AmapPoi:
    return AmapPoi(poi_id=poi_id, name=name, type=type_str, lng=104.0, lat=30.6)


class ScriptedSearchProvider:
    """只为 `search_poi` 造的桩：返回**预设顺序**的候选，并记下每次的 `limit` 与 `center`。"""

    name = "scripted"

    def covers(self, city: str | None) -> bool:  # pragma: no cover - 这些用例不判覆盖
        """`AmapProvider` 协议要求（D67-B）。本文件不走工具循环。"""
        return True

    def __init__(self, pois: list[AmapPoi]) -> None:
        self.pois = pois
        self.limits: list[int] = []
        self.centers: list[str | None] = []

    async def search_poi(
        self,
        keyword: str,
        city: str | None = None,
        limit: int = 10,
        center: str | None = None,
    ) -> list[AmapPoi]:
        self.limits.append(limit)
        self.centers.append(center)
        return self.pois[:limit]

    async def get_poi(self, poi_id: str):  # pragma: no cover - 用不到
        return next((p for p in self.pois if p.poi_id == poi_id), None)

    async def get_weather(self, city: str, day: date):  # pragma: no cover - 用不到
        raise NotImplementedError

    async def calc_distance(self, origin, dest):  # pragma: no cover - 用不到
        raise NotImplementedError


def _search_with(provider: ScriptedSearchProvider) -> tuple[str, int]:
    """跑一次 search_poi，返回 (给模型的文本, 池子大小)。"""
    tools = {t.name: t for t in build_amap_tools(provider)}  # type: ignore[arg-type]

    async def scene() -> tuple[str, int]:
        with poi_pool_scope() as pool:
            text = await tools["search_poi"].ainvoke({"keyword": "都江堰"})  # type: ignore[attr-defined]
            return text, len(pool)

    return run(scene())


def test_search_fetches_more_than_it_shows() -> None:
    """🔴 三段式：内部取 `SEARCH_FETCH` 条 → 重排 → 只给模型看 `MAX_CANDIDATES` 条。

    为什么要多取：实测搜"都江堰"前 8 条里只有 2 条是景点，取 8 条就**没得挑**。
    多取的条**不进 prompt** —— token 成本一点不变，只是让"把景点挑到前排"有材料。
    """
    pois = [make_poi(f"P{i}", f"地点{i}", "风景名胜;风景名胜;风景名胜") for i in range(SEARCH_FETCH)]
    provider = ScriptedSearchProvider(pois)
    text, pool_size = _search_with(provider)

    assert provider.limits == [SEARCH_FETCH], "内部取回量不是 SEARCH_FETCH"
    assert SEARCH_FETCH > MAX_CANDIDATES, "多取必须真的多于展示，否则这个设计没意义"
    assert len(text.splitlines()) == 1 + MAX_CANDIDATES, "展示条数不是 MAX_CANDIDATES"


def test_pool_keeps_the_full_result_despite_truncated_display() -> None:
    """池子记**全量**，截断只发生在展示层。

    反过来做（池子只记展示的 8 条）会让"模型引用了一条它其实搜到过、
    只是没被展示的 POI"被判成"编造" —— 校验层会冤枉模型。
    """
    pois = [make_poi(f"P{i}", f"地点{i}", "风景名胜;风景名胜;风景名胜") for i in range(SEARCH_FETCH)]
    _, pool_size = _search_with(ScriptedSearchProvider(pois))
    assert pool_size == SEARCH_FETCH


def test_search_shows_sites_before_service_facilities() -> None:
    """🔴 重排生效：即使高德把服务设施排在前排，展示时景点在前。

    输入顺序照抄**真实样本**（`fixtures/amap/pool_quality/kw_都江堰.json`）：
    前三条是市政府 / 客运中心 / 公交站，景点在第 4 位。
    """
    provider = ScriptedSearchProvider(
        [
            make_poi("S1", "都江堰市人民政府", "政府机构及社会团体;政府机关;区县级政府及事业单位"),
            make_poi("S2", "都江堰市客运中心", "交通设施服务;长途汽车站;长途汽车站"),
            make_poi("S3", "都江堰快铁站(公交站)", "交通设施服务;公交车站;公交车站相关"),
            make_poi("S4", "都江堰景区", "风景名胜;风景名胜;国家级景点"),
        ]
    )
    text, _ = _search_with(provider)
    first = text.splitlines()[1]
    assert "都江堰景区" in first, f"重排没生效，第一条是：{first}"


def test_multi_value_type_is_not_demoted() -> None:
    """🔴 多值 `type` 取**最优**档 —— 宽窄巷子不能被当成购物场所排到后面。

    真实值：`购物服务;特色商业街;特色商业街|风景名胜;风景名胜相关;旅游景点`。
    只看第一段（购物服务）会让一个顶级景点被"景点"挤下去。
    """
    provider = ScriptedSearchProvider(
        [
            make_poi("S1", "某地铁站", "交通设施服务;地铁站;地铁站"),
            make_poi("S2", "宽窄巷子景区", "购物服务;特色商业街;特色商业街|风景名胜;风景名胜相关;旅游景点"),
            make_poi("S3", "成都太古里", "购物服务;商场;购物中心"),
        ]
    )
    text, _ = _search_with(provider)
    lines = text.splitlines()[1:]
    assert "宽窄巷子景区" in lines[0], f"宽窄巷子被降级了，第一条是：{lines[0]}"


def test_search_labels_each_candidate_with_type() -> None:
    """每行要带类型标签 —— 量不出收益但成本极低的"零成本保险"。

    模型知道第 3 条是"地铁站"，就不会把它排进行程。断言到标签**内容**级，
    不只是"有方括号"，否则改错取哪一段也发现不了。
    """
    provider = ScriptedSearchProvider(
        [
            make_poi("S1", "都江堰景区", "风景名胜;风景名胜;国家级景点"),
            make_poi("S2", "某火锅店", "餐饮服务;中餐厅;火锅店"),
            make_poi("S3", "某售票处", "生活服务;售票处;公园景点售票处"),
        ]
    )
    text, _ = _search_with(provider)
    assert "[国家级景点]" in text
    assert "[火锅店]" in text
    assert "[公园景点售票处]" in text


def test_search_description_warns_service_facilities_are_not_sights(
    tools: dict[str, object],
) -> None:
    """docstring 必须**明说**后半段可能是服务设施、不要排进行程。

    这条约束写在工具描述里（模型决定要不要调它时就会读到），
    不是写在 system prompt —— 那是"把约束写在它生效的地方"（见模块 docstring ③）。
    """
    desc = tools["search_poi"].description  # type: ignore[attr-defined]
    assert "服务设施" in desc
    assert "不要排进行程" in desc


# ══════════════════════════════════════════════════════════════
#  三、get_weather
# ══════════════════════════════════════════════════════════════


def test_weather_in_window(tools: dict[str, object]) -> None:
    text = run(tools["get_weather"].ainvoke({"date_str": "2026-09-16"}))  # type: ignore[attr-defined]
    assert "2026-09-16" in text
    assert "℃" in text
    assert "无法判定" not in text


def test_weather_out_of_window_forbids_guessing(tools: dict[str, object]) -> None:
    """超出预报窗口时，必须明说"不要编造" + 指示"留空"。"""
    text = run(tools["get_weather"].ainvoke({"date_str": "2026-10-01"}))  # type: ignore[attr-defined]
    assert "无法判定" in text
    assert "预报窗口" in text
    assert "不要编造" in text


def test_weather_accepts_common_date_formats(tools: dict[str, object]) -> None:
    """模型会写 `2026/09/16`、`20260916`。这些要能解析 —— 但不猜。"""
    for raw in ("2026/09/16", "2026.09.16", "20260916"):
        text = run(tools["get_weather"].ainvoke({"date_str": raw}))  # type: ignore[attr-defined]
        assert "2026-09-16" in text, f"{raw} 没被解析成 2026-09-16"
        assert "无法判定" not in text


def test_weather_rejects_unparseable_date(tools: dict[str, object]) -> None:
    """🔴 解析不了就报错，**不猜**。

    猜错的后果特别隐蔽：静默查到**另一天**的天气，而输出看起来完全正常。
    """
    text = run(tools["get_weather"].ainvoke({"date_str": "下周三"}))  # type: ignore[attr-defined]
    assert "日期参数有问题" in text
    assert " YYYY-MM-DD" in text


# ══════════════════════════════════════════════════════════════
#  四、calc_distance
# ══════════════════════════════════════════════════════════════


def test_distance_between_two_searched_pois(tools: dict[str, object]) -> None:
    async def scene() -> str:
        with poi_pool_scope():
            await tools["search_poi"].ainvoke({"keyword": "武侯祠"})  # type: ignore[attr-defined]
            await tools["search_poi"].ainvoke({"keyword": "熊猫基地"})  # type: ignore[attr-defined]
            return await tools["calc_distance"].ainvoke(  # type: ignore[attr-defined]
                {"from_poi_id": "B001C07VJ2", "to_poi_id": "B001C7WE5S"}
            )

    text = run(scene())
    assert "成都武侯祠博物馆" in text
    assert "成都大熊猫繁育研究基地" in text
    assert "km" in text
    assert "分钟" in text


def test_distance_rejects_ids_outside_pool(tools: dict[str, object]) -> None:
    """🔴 池子里没有的 id 必须拒绝，并指示"先搜"。

    这是把封闭世界**走通**的关键一环：模型没法用记忆里的 id 算距离。
    """

    async def scene() -> str:
        with poi_pool_scope():
            return await tools["calc_distance"].ainvoke(  # type: ignore[attr-defined]
                {"from_poi_id": "MADE_UP_ID", "to_poi_id": "ALSO_FAKE"}
            )

    text = run(scene())
    assert "不在候选池里" in text
    assert "MADE_UP_ID" in text
    assert "先用 search_poi" in text
    assert "不要自己推算" in text


def test_distance_without_pool_is_rejected_not_crashed(tools: dict[str, object]) -> None:
    """没开池子时不能崩，也不能"算出来" —— 必须拒绝。"""
    assert current_pool() is None
    text = run(
        tools["calc_distance"].ainvoke(  # type: ignore[attr-defined]
            {"from_poi_id": "B001C07VJ2", "to_poi_id": "B001C7WE5S"}
        )
    )
    assert "不在候选池里" in text


def test_candidate_limit_is_module_level_not_model_controlled() -> None:
    """候选数量是**模块常量**，不暴露给模型当参数。

    让模型设 `limit` 只是多一个能设错的地方 —— 它并不知道多给几个对决策更好。
    """
    assert MAX_CANDIDATES == 8


# ══════════════════════════════════════════════════════════════
#  ⑤ 搜索中心点（D75）
# ══════════════════════════════════════════════════════════════

# 实测拿到的真实坐标 —— 用真数据而不是编的，这样"抗离群"那条断言有实际意义
KUANZHAI = (104.053307, 30.663869)  # 宽窄巷子景区（青羊区，市中心）
WUHOU = (104.047992, 30.646168)  # 成都武侯祠博物馆（武侯区，市中心偏南）
DUJIANGYAN = (103.610529, 31.003363)  # 都江堰景区（远郊，离市区 56 km）


def _at(poi_id: str, lng: float, lat: float) -> AmapPoi:
    return AmapPoi(poi_id=poi_id, name=f"测试点{poi_id}", lng=lng, lat=lat)


def test_search_center_is_none_without_active_pool() -> None:
    """没有活动池子时不能炸 —— 工具可能被单独调用做调试。"""
    assert current_pool() is None
    assert _search_center() is None


def test_search_center_is_none_when_pool_empty() -> None:
    """第一次搜索时池子是空的 → 不传中心点 = **与改动前逐字一致**。

    这条是"改动默认等于旧行为"的守卫：新会话的第一句搜索，
    请求里不该多出 `location`/`radius`。
    """
    with poi_pool_scope():
        assert _search_center() is None


def test_search_center_is_medoid_not_centroid() -> None:
    """🔴 中心点必须是 medoid 而不是质心 —— 这是整件事的关键取舍。

    三个锚点是实测拿到的真坐标（市区两个 + 远郊一个）。拿它们的**质心**
    搜「公园」会落在郫都区（到两边各 28 km），返回郫都区的公园 —— **比不加还差**。
    medoid 选中宽窄巷子，返回的是市中心公园（实测 0.8~2.6 km）。
    """
    with poi_pool_scope() as pool:
        pool.record([_at("A", *KUANZHAI), _at("B", *WUHOU), _at("C", *DUJIANGYAN)])
        assert _search_center() == "104.053307,30.663869"

        # 顺带把"质心会落在哪儿"写进断言 —— 否则下一个人改成质心，
        # 这条测试还是会过（因为 medoid 恰好也不变），但看不出为什么不能改
        centroid = (
            sum(p[0] for p in (KUANZHAI, WUHOU, DUJIANGYAN)) / 3,
            sum(p[1] for p in (KUANZHAI, WUHOU, DUJIANGYAN)) / 3,
        )
        assert haversine_km(centroid, KUANZHAI) > 10, (
            "质心到最近的锚点也有 10 km+ —— 它落在市区与都江堰之间的中间地带，"
            "拿它当中心点搜出来的东西谁的行程都不挨着"
        )


def test_search_center_tie_prefers_first_seen() -> None:
    """两个锚点平局时取**先出现的** —— 先搜到的通常是主景点。

    实测「宽窄巷子 + 都江堰」两点到彼此距离相同（平局），
    Python 的 `min` 返回第一个最小值 → 选中宽窄巷子 → 结果是对的。
    """
    with poi_pool_scope() as pool:
        pool.record([_at("A", *KUANZHAI), _at("C", *DUJIANGYAN)])
        assert _search_center() == "104.053307,30.663869"


def test_second_search_carries_center_from_pool() -> None:
    """端到端：第一次搜索不带中心点，第二次带上（锚点来自第一次的结果）。

    也顺带钉住 `center` 是在 `record` **之前**算的 —— 池子里不该混进本次结果。
    """
    provider = ScriptedSearchProvider([_at("A", *KUANZHAI)])
    tools = {t.name: t for t in build_amap_tools(provider)}  # type: ignore[arg-type]

    async def scene() -> None:
        with poi_pool_scope():
            await tools["search_poi"].ainvoke({"keyword": "宽窄巷子"})  # type: ignore[attr-defined]
            await tools["search_poi"].ainvoke({"keyword": "公园"})  # type: ignore[attr-defined]

    run(scene())
    assert provider.centers[0] is None, "首次搜索池子是空的，不该有中心点"
    assert provider.centers[1] == "104.053307,30.663869", "第二次该用池子里的锚点当中心"
