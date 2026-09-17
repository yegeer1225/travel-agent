"""收录库（D70）的 store 契约测试。

⚠️ **这里测的是内存替身**：SQL 版 `SpotRepo` 要连真库，而 tests 不连库（`requirements.txt`
里的纪律）—— SQL 路径由 `scripts/verify_mysql_api.py` 在 MySQL 起来后验。
所以本文件重点守两件内存替身能守的事：

1. **排序规则**（前缀命中 → 评分降序 → 名称），它是**不报错的那种错**：
   顺序不对时接口照样 200，只是页面上顺序怪，没人会去查
2. **与 SQL 版同签名**（D31/`memory.py` 的老纪律）——
   少一个方法时，路由层会在"测试绿、生产 500"之间撒谎
"""

from __future__ import annotations

import inspect

from app.schemas import AmapPoi
from app.store.memory import InMemorySpotStore
from app.store.repo import SpotRepo


def _poi(pid: str, name: str, *, rating: str | None = "4.5", alias: list[str] | None = None,
         city: str = "成都市", district: str = "青羊区") -> AmapPoi:
    return AmapPoi(
        poi_id=pid, name=name, alias=alias or [], lng=104.05, lat=30.64,
        cityname=city, adname=district, rating=rating, typecode="110200",
    )


def test_memory_store_has_same_public_api_as_sql_repo():
    """同签名纪律：内存替身不能少方法（少了 = 测试绿、生产 AttributeError）。"""
    public = {
        name
        for name, _ in inspect.getmembers(SpotRepo, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    fake = {
        name
        for name, _ in inspect.getmembers(InMemorySpotStore, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == fake, f"SQL 版独有：{public - fake}；内存版独有：{fake - public}"


def test_search_prefix_match_ranks_first():
    """前缀命中排前面，**哪怕评分更低** —— 否则搜「成都」时前缀命中的『成都博物馆』
    会被评分更高的『老成都茶馆』压到后面。"""
    store = InMemorySpotStore([
        _poi("B1", "老成都茶馆", rating="4.9"),   # 含「成都」但非前缀
        _poi("B2", "成都博物馆", rating="4.5"),   # 前缀命中
    ])
    items, total = store.search("成都")
    assert total == 2
    assert [c.poi_id for c in items] == ["B2", "B1"]


def test_search_rating_desc_then_name():
    store = InMemorySpotStore([
        _poi("B1", "人民公园", rating="4.5"),
        _poi("B2", "文化公园", rating="4.8"),
        _poi("B3", "百花潭公园", rating="4.8"),
    ])
    items, _ = store.search("公园")
    # 都非前缀命中 → 评分降序；同分按名称字典序。
    # ⚠️ 「字典序」在中文里 = **码点序**（文 U+6587 < 百 U+767E），是拼音序**不是** ——
    #    MySQL 的 utf8mb4_0900_ai_ci 同样是码点序，所以内存版与 SQL 版一致。
    #    真实用户看不出哪个更"对"，但两侧必须一致，否则测试绿而生产乱序。
    assert [c.poi_id for c in items] == ["B2", "B3", "B1"]


def test_search_missing_rating_sorts_last_not_zero():
    """🔴 缺评分排最后，**不是当 0 分**（与 `AmapPoi` 的 `str | [] → None` 同一纪律）。"""
    store = InMemorySpotStore([
        _poi("B1", "无评分公园", rating=None),
        _poi("B2", "低分公园", rating="1.0"),
    ])
    items, _ = store.search("公园")
    assert [c.poi_id for c in items] == ["B2", "B1"]


def test_search_matches_alias_and_city():
    store = InMemorySpotStore([
        _poi("B1", "宽窄巷子景区", alias=["宽窄巷子", "少城"]),
        _poi("B2", "成都武侯祠博物馆", alias=["武侯祠"]),
    ])
    assert store.search("少城")[0][0].poi_id == "B1"       # 只在别名里
    assert store.search("武侯祠")[0][0].poi_id == "B2"     # 名字里有
    assert store.search("宽窄", city="成都")[1] == 1        # 城市用包含匹配
    assert store.search("宽窄", city="杭州")[1] == 0        # 别的城市 → 空，不报错


def test_search_paging():
    store = InMemorySpotStore([_poi(f"B{i}", f"公园{i}") for i in range(1, 6)])
    page1, total = store.search("公园", limit=2, offset=0)
    page2, _ = store.search("公园", limit=2, offset=2)
    assert total == 5
    assert len(page1) == 2 and len(page2) == 2
    assert {c.poi_id for c in page1}.isdisjoint({c.poi_id for c in page2})


def test_upsert_is_idempotent_and_reports_counts():
    store = InMemorySpotStore()
    pois = [_poi("B1", "宽窄巷子景区"), _poi("B2", "成都武侯祠博物馆")]
    assert store.upsert_many(pois) == (2, 0)
    assert store.count() == 2
    # 重跑：全部算更新，条数不变（收录脚本靠这个做幂等）
    assert store.upsert_many(pois) == (0, 2)
    assert store.count() == 2


def test_get_unknown_returns_none():
    store = InMemorySpotStore([_poi("B1", "宽窄巷子景区")])
    assert store.get("B1") is not None
    assert store.get("NOT_EXIST") is None


def test_spot_card_does_not_leak_internal_fields():
    """收录库内部存了 open_time/type/adcode（留给将来导出 mock 池），
    但**对外契约 `SpotCard` 不许漏它们** —— 契约是精简卡，不是内部结构。"""
    store = InMemorySpotStore([_poi("B1", "宽窄巷子景区")])
    card = store.get("B1")
    assert card is not None
    dumped = card.model_dump()
    for leaked in ("open_time", "type", "adcode", "alias", "tel", "adname", "cityname"):
        assert leaked not in dumped, f"{leaked} 泄漏进了 SpotCard"
