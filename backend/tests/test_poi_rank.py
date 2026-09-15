"""`poi_rank` 的测试 —— 候选清单的排序规则。

两条测试性质完全不同，别混着看：

| 类型 | 例子 | 盯什么 |
|---|---|---|
| **规则测试** | `majors_of` 会不会漏拆多值 | 我自己写的逻辑有没有错 |
| **实测回归** | 真实样本重排后 top-8 是不是全可游玩 | **高德有没有变** |

第二类更重要：它把 2026-09-15 那次实测的结论**钉住了**。
高德以后改了排序或改了 `type` 格式，这些测试会红 —— 红了不代表代码坏了，
代表**当初的实测结论过期了，要重新量一遍**。这正是我们想要的提醒。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.providers.amap import parse_poi
from app.schemas import AmapPoi
from app.tools.poi_rank import (
    RANK_BY_MAJOR,
    RANK_FALLBACK,
    label_of,
    majors_of,
    rank_of,
    rank_pois,
)

FIXTURES = Path(__file__).parent / "fixtures" / "amap"
POOL_QUALITY = FIXTURES / "pool_quality"


def poi(name: str, type_str: str | None, poi_id: str = "") -> AmapPoi:
    """造一个最小可用的 POI。只填排序需要的字段。"""
    return AmapPoi(
        poi_id=poi_id or name,
        name=name,
        type=type_str,
        lng=104.0,
        lat=30.6,
    )


def load_pool(keyword: str) -> list[AmapPoi]:
    """读**真实采下来的**原始响应，走真实解析 —— 不是手写的理想 JSON。"""
    payload: dict[str, Any] = json.loads(
        (POOL_QUALITY / f"kw_{keyword}.json").read_text(encoding="utf-8")
    )
    return [p for p in (parse_poi(x) for x in payload.get("pois") or []) if p is not None]


# ══════════════════════════════════════════════════════════════
#  一、majors_of：多值拆分（**这个模块最容易写错的地方**）
# ══════════════════════════════════════════════════════════════


def test_majors_of_splits_multi_value() -> None:
    """🔴 核心回归：`type` 可以是 `|` 分隔的多值。

    宽窄巷子景区的真实值就是多值的。漏拆这一步的后果不是"排错几个"，
    是**把顶级景点判成购物场所**（它的第一段是"购物服务"，
    第二段才是"风景名胜"）。
    """
    raw = "购物服务;特色商业街;特色商业街|风景名胜;风景名胜相关;旅游景点"
    assert majors_of(raw) == ["购物服务", "风景名胜"]


def test_majors_of_handles_three_segments_values() -> None:
    """实测里还有三段的：`050117|050102|110200`（火锅店 + 川菜 + 风景名胜）。"""
    raw = "餐饮服务;中餐厅;火锅店|餐饮服务;中餐厅;四川菜(川菜)|风景名胜;风景名胜;风景名胜"
    assert majors_of(raw) == ["餐饮服务", "风景名胜"]


@pytest.mark.parametrize("bad", [None, "", "   ", "|", ";;", "|;"])
def test_majors_of_survives_garbage(bad: str | None) -> None:
    """空值不抛异常、不返回 `['']` —— 返回空列表，让 `rank_of` 走兜底档。"""
    assert majors_of(bad) == []


def test_majors_of_dedupes_but_keeps_order() -> None:
    """同段重复只留一个，且保序（顺序影响 `label_of` 取哪一段）。"""
    assert majors_of("餐饮服务;中餐厅|餐饮服务;中餐厅;火锅店") == ["餐饮服务"]


# ══════════════════════════════════════════════════════════════
#  二、rank_of：分层与兜底
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("type_str", "expected"),
    [
        ("风景名胜;风景名胜;国家级景点", 0),
        ("公园广场;公园;公园", 0),
        ("科教文化服务;博物馆;博物馆", 1),
        ("体育休闲服务;休闲场所;游乐场", 2),
        ("餐饮服务;中餐厅;火锅店", 3),
        ("购物服务;特色商业街;特色商业街", 3),
        ("住宿服务;宾馆酒店;宾馆酒店", 4),
        ("交通设施服务;地铁站;地铁站", RANK_FALLBACK),
        ("地名地址信息;交通地名;道路名", RANK_FALLBACK),
        ("生活服务;售票处;公园景点售票处", RANK_FALLBACK),
        (None, RANK_FALLBACK),
    ],
)
def test_rank_of_layers(type_str: str | None, expected: int) -> None:
    assert rank_of(type_str) == expected


def test_rank_of_takes_best_segment_for_multi_value() -> None:
    """多值取**最优**档：`购物服务|风景名胜` 是 0（景点），不是 3（购物）。

    这是"重排不会误杀宽窄巷子"的机器化保证 —— 如果哪天有人把 `min` 改成 `max`
    或改成"只看第一段"，这条会红。
    """
    assert rank_of("购物服务;特色商业街;特色商业街|风景名胜;风景名胜相关;旅游景点") == 0


def test_rank_of_unknown_major_is_worst_not_dropped() -> None:
    """**不认识的大类必须排最后，而不是报错或丢弃。**

    高德会新增大类（实测里已经见到"农林牧渔基地"这种）。
    遇到没见过的就排最后 → 顶多它不好找；报错或丢弃 → 整个搜索废掉。
    """
    assert rank_of("某个还没出现过的大类;中类;小类") == RANK_FALLBACK


def test_fallback_is_worse_than_every_named_rank() -> None:
    """兜底档必须 ≥ 所有具名档，否则"不认识"会插队到已知地点前面。"""
    assert RANK_FALLBACK >= max(RANK_BY_MAJOR.values())


@pytest.mark.parametrize(
    "type_str",
    [
        "科教文化服务;中学;中学",
        "科教文化服务;学校;学校",
        "科教文化服务;驾校;驾校",
    ],
)
def test_rank_of_rejects_non_visit_leaves(type_str: str) -> None:
    """🔴 末级否决：中学 / 驾校 虽然大类是「科教文化服务」，但**不是游览对象**。

    这一条来自实测 —— 搜「都江堰」时前 8 里混进了 `四川省都江堰中学` 和
    `都江堰考场[驾校]`，只因为它们和博物馆共享同一个一级大类。
    """
    assert rank_of(type_str) == RANK_FALLBACK


def test_non_visit_leaf_only_kills_its_own_segment() -> None:
    """多值里一段被否决，另一段是景点 → **仍然按景点排**（否决不做全局传染）。"""
    assert rank_of("科教文化服务;学校;学校|风景名胜;风景名胜;风景名胜") == 0


def test_label_still_shows_a_rejected_leaf() -> None:
    """否决**只影响档位，不影响标签** —— 模型还是要看到「驾校」才能跳过它。

    如果这里返回 `None`，那一行会看起来像"没有类型"，模型反而可能去选它。
    """
    assert label_of("科教文化服务;驾校;驾校") == "驾校"


def test_majors_of_ignores_the_veto() -> None:
    """`majors_of` 是**描述**（它属于什么），不参与否决。

    两个问题不一样：`rank_of` 回答"能不能当站点"，`majors_of` 回答"它是什么"。
    混在一起的话，日志和调试信息里会看不出"这条其实是被否决的"。
    """
    assert majors_of("科教文化服务;驾校;驾校") == ["科教文化服务"]


# ══════════════════════════════════════════════════════════════
#  三、label_of：给模型看的类型标签
# ══════════════════════════════════════════════════════════════


def test_label_of_takes_leaf_not_major() -> None:
    """取**末级**：模型需要的是「火锅店」，不是「餐饮服务」。"""
    assert label_of("餐饮服务;中餐厅;火锅店") == "火锅店"


def test_label_of_takes_leaf_of_best_segment() -> None:
    """多值取最优段的末级 —— 宽窄巷子该显示「旅游景点」而不是「特色商业街」。"""
    assert label_of("购物服务;特色商业街;特色商业街|风景名胜;风景名胜相关;旅游景点") == "旅游景点"


def test_label_of_two_segment_value() -> None:
    assert label_of("风景名胜;公园广场;公园") == "公园"


@pytest.mark.parametrize("bad", [None, "", "   ", ";;"])
def test_label_of_none_when_nothing_usable(bad: str | None) -> None:
    """取不出标签时返回 `None` —— 调用方据此**整段不输出**，而不是输出 `[]`。"""
    assert label_of(bad) is None


# ══════════════════════════════════════════════════════════════
#  四、rank_pois：排序本身的三条不变量
# ══════════════════════════════════════════════════════════════


def test_rank_pois_never_drops_anything() -> None:
    """**重排不删条目** —— 这是它相对"过滤"的核心优势。

    池子（校验层的数据来源）必须保持全量：分类判错的代价只能是"排后面"，
    不能是"消失"。
    """
    pois = [poi("a", "交通设施服务;地铁站;地铁站"), poi("b", "风景名胜;风景名胜;风景名胜")]
    assert len(rank_pois(pois)) == len(pois)


def test_rank_pois_is_stable_within_a_rank() -> None:
    """同档内**保持高德原始顺序**。

    高德的原始顺序里含有我们读不出来的信号（它自己的相关性打分）。
    实测支持这点：搜「都江堰」时目标本来就在第 1 条，档内重排只会把它搞乱。
    """
    pois = [
        poi("都江堰景区", "风景名胜;风景名胜;国家级景点"),
        poi("都江夜堰", "风景名胜;风景名胜相关;旅游景点"),
        poi("都江堰水利工程景区", "风景名胜;风景名胜相关;旅游景点"),
    ]
    assert [p.name for p in rank_pois(pois)] == [p.name for p in pois]


def test_rank_pois_puts_landmarks_before_stations() -> None:
    """分类判错的容错：即使原序里车站排在前面，重排后景点在前。"""
    pois = [
        poi("都江堰市", "地名地址信息;普通地名;区县级地名"),
        poi("都江堰市客运中心", "交通设施服务;长途汽车站;长途汽车站"),
        poi("都江堰快铁站(公交站)", "交通设施服务;公交车站;公交车站相关"),
        poi("都江堰景区", "风景名胜;风景名胜;国家级景点"),
    ]
    assert rank_pois(pois)[0].name == "都江堰景区"


# ══════════════════════════════════════════════════════════════
#  五、实测回归 —— 这一节红了代表「高德变了」，不是「代码坏了」
# ══════════════════════════════════════════════════════════════

#: 能被排进行程当一站的档位（景点本体 / 文化场馆 / 体育休闲）。
_SITE_RANKS = (0, 1, 2)


@pytest.mark.parametrize("keyword", ["都江堰", "宽窄巷子", "武侯祠", "熊猫基地"])
def test_real_sample_rerank_fills_window_with_sites(keyword: str) -> None:
    """🔴 **实测回归**：重排后窗口里的可站点数 = `min(池内可站点数, 8)`。

    2026-09-15 实测（口径：可站点 = 档位 0/1/2）：

    | 关键词 | 池内可站点 | 重排前窗口 | 重排后窗口 |
    |---|---|---|---|
    | 都江堰 | 8 | **2** / 8 | **8** / 8 |
    | 宽窄巷子 | 6 | 3 / 8 | **6** / 8 |
    | 武侯祠 | 8 | **2** / 8 | **8** / 8 |
    | 熊猫基地 | 12 | 5 / 8 | **8** / 8 |

    ⚠️ 两条容易读错的地方，都写在这里免得下次误判：

    1. **宽窄巷子永远到不了 8/8，这不是 bug** —— 搜它的 20 条返回里能当站点的
       本来就只有 6 条，剩下 14 条是酒店 / 地铁站 / 停车场 / 餐饮。
       **"池子里没有的东西，重排变不出来"** —— 所以断言是"窗口被站点填满"，
       不是"窗口全是站点"。后者会被误读成"排完序就没噪音了"。
    2. **档位 3 的条目（购物 / 餐饮）不算"可站点"只是个口径**，不代表它们没用 ——
       `窄巷子` 被高德归成"餐饮服务"，但它就是宽窄巷子的组成部分。
       这正是**不选"过滤"选"重排"**的理由：判错的代价只能是"排后面"，不能是"消失"。
    """
    pois = load_pool(keyword)
    assert pois, f"fixture 缺样本：kw_{keyword}.json"

    sites = [p for p in pois if rank_of(p.type) in _SITE_RANKS]
    assert sites, f"「{keyword}」的样本里一条可站点都没有，样本选得不对"

    window = rank_pois(pois)[:8]
    in_window = [p for p in window if rank_of(p.type) in _SITE_RANKS]

    assert len(in_window) == min(len(sites), 8), (
        f"「{keyword}」窗口没被站点填满：池内 {len(sites)} 条，窗口里只进来 {len(in_window)} 条"
    )


def test_real_sample_rerank_is_strictly_better() -> None:
    """重排必须**严格改善**窗口，用两个独立口径各算一次。

    只断言"重排后很好"会被"样本恰好简单"骗过。两边都算才能证明这次改动确实修了东西。
    """

    def site_count(pois: list[AmapPoi]) -> int:
        return sum(1 for p in pois[:8] if rank_of(p.type) in _SITE_RANKS)

    def mean_rank(pois: list[AmapPoi]) -> float:
        window = pois[:8]
        return sum(rank_of(p.type) for p in window) / len(window)

    keywords = ["都江堰", "宽窄巷子", "武侯祠", "熊猫基地", "寺庙", "古迹"]
    before = after = 0
    rank_before = rank_after = 0.0
    for kw in keywords:
        pois = load_pool(kw)
        reranked = rank_pois(pois)
        before += site_count(pois)
        after += site_count(reranked)
        rank_before += mean_rank(pois)
        rank_after += mean_rank(reranked)

    assert after > before, f"重排没有让更多站点进窗口：{before} → {after}"
    assert rank_after < rank_before, (
        f"重排没有降低窗口平均档位：{rank_before:.2f} → {rank_after:.2f}"
    )


def test_real_sample_loses_nothing() -> None:
    """重排前后**条目集合完全相同** —— 用真实样本再验一次"不删条目"。"""
    for keyword in ["都江堰", "宽窄巷子", "武侯祠", "古迹"]:
        pois = load_pool(keyword)
        assert {p.poi_id for p in rank_pois(pois)} == {p.poi_id for p in pois}


def test_real_sample_window_has_no_schools_or_driving_schools() -> None:
    """实测回归：搜「都江堰」重排后的窗口里**不该出现中学 / 驾校**。

    这两条（`四川省都江堰中学` / `都江堰考场`）是真实样本里的，曾经因为和博物馆
    共享「科教文化服务」这个一级大类而挤进前 8 —— 真人看到"都江堰中学"当景点会笑出来。
    """
    window = rank_pois(load_pool("都江堰"))[:8]
    bad = [p.name for p in window if (p.type or "").rsplit(";", 1)[-1] in {"中学", "驾校", "学校"}]
    assert not bad, f"窗口里出现了非游览对象：{bad}"
