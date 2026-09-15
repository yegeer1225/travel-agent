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
from app.tools import build_amap_tools, current_pool, poi_pool_scope
from app.tools.amap_tools import MAX_CANDIDATES

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
