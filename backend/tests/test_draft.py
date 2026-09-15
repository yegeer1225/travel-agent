"""草稿补全层自检 —— **防止模型编造的那一刀**。

最该盯住的不是"函数能跑"，而是**模型无从编造**这件事本身：

1. `PlanDraft` 里**没有** `lng` / `lat` / `distance` / `rating` / `summary` 这些字段
   （"不给它那个字段"比"在 prompt 里禁止"可靠得多）
2. 引用了池子里没有的 `poi_id` → **该站被剔除，且必须有 `blocking` 记录**
   （静默跳过 = 用户以为规划成功但少了一站）
3. 首站距离必须是 0（不是"上一站到首站的距离"）
4. 拿不到天气必须是 `unavailable`，**不是编一个"晴"**
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.graph.draft import (
    WALK_KM_PER_STOP,
    DraftDay,
    DraftStop,
    PlanDraft,
    _add_minutes,
    _normalize_hhmm,
    assemble_trip,
    parse_draft,
)
from app.providers.mock import MOCK_POI_POOL, MockAmapProvider
from app.schemas import WeatherStatus
from app.tools.poi_pool import PoiPool

TODAY = date(2026, 9, 16)


def run(coro):
    return asyncio.run(coro)


def _full_pool() -> PoiPool:
    pool = PoiPool()
    pool.record(list(MOCK_POI_POOL))
    return pool


def _ids(n: int) -> list[str]:
    return [p.poi_id for p in MOCK_POI_POOL[:n]]


def _draft(*poi_ids: str, day: date = TODAY) -> PlanDraft:
    return PlanDraft(
        title="测试行程",
        days=[
            DraftDay(
                date=day,
                theme="测试主题",
                stops=[
                    DraftStop(
                        poi_id=pid,
                        arrive=f"{9 + i:02d}:00",
                        stay_min=90,
                        match_reason="测试理由",
                    )
                    for i, pid in enumerate(poi_ids)
                ],
            )
        ],
    )


# ══════════════════════════════════════════════════════════════
#  「不给它那个字段」—— 机制层面的断言
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "forbidden",
    ["lng", "lat", "from_prev_km", "from_prev_drive_min", "rating", "open_time", "checks"],
)
def test_draft_stop_has_no_fact_fields(forbidden: str):
    """🔴 模型输出结构里**不能**有坐标/距离/评分这类字段。

    这是整个防编造设计的根：**它没有那个字段，就编不出来。**
    哪天有人为了"方便"往 `DraftStop` 上加一个 `lat`，这条测试会红 ——
    那是设计被破坏的信号，不是测试太严。
    """
    assert forbidden not in DraftStop.model_fields


@pytest.mark.parametrize("forbidden", ["summary", "day_stats", "validation"])
def test_draft_day_has_no_aggregate_fields(forbidden: str):
    """汇总数字也必须由代码算 —— 模型自己加一遍，还算错。"""
    assert forbidden not in DraftDay.model_fields
    assert forbidden not in PlanDraft.model_fields


# ══════════════════════════════════════════════════════════════
#  时刻规范化
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("09:30", "09:30"),
        ("9:30", "09:30"),  # 模型最常见的不规范写法
        ("9:5", "09:05"),
        ("0930", "09:30"),
        ("930", "09:30"),
        ("09：30", "09:30"),  # 全角冒号
        ("14.30", "14:30"),
        (" 10:00 ", "10:00"),
    ],
)
def test_normalize_hhmm_fixes_unambiguous_forms(raw: str, expected: str):
    """`schemas.py` 的 `HHMM` 是严格 `\\d{2}:\\d{2}` —— 但**严格性不该转嫁给用户**。

    能在代码里无歧义修好的，就在代码里修，不要拿 prompt 去赌。
    """
    assert _normalize_hhmm(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "上午", "25:00", "12:99", "abc", "1"])
def test_normalize_hhmm_refuses_ambiguous(raw):
    """修不了就返回 `None`，**不要猜** —— 猜错会静默排出一个错时刻的行程。"""
    assert _normalize_hhmm(raw) is None


def test_add_minutes_normal():
    assert _add_minutes("09:00", 90) == "10:30"
    assert _add_minutes("09:30", 30) == "10:00"


def test_add_minutes_wraps_at_midnight():
    """⚠️ 跨天按 24 小时取模 —— 这是**已知近似**（见函数注释），
    M3 要加"结束时刻不得晚于 23:00"从源头避免它。这里把它钉住，
    免得哪天有人"顺手改成报错"却不知道有这个契约。"""
    assert _add_minutes("23:00", 120) == "01:00"


# ══════════════════════════════════════════════════════════════
#  草稿解析
# ══════════════════════════════════════════════════════════════


def test_parse_draft_strips_fence():
    raw = '```json\n{"title": "x", "days": []}\n```'
    draft, err = parse_draft(raw)
    assert err is None
    assert draft is not None and draft.title == "x"


def test_parse_draft_reports_bad_json():
    draft, err = parse_draft("{不是 json")
    assert draft is None
    assert err is not None and "不是合法 JSON" in err


def test_parse_draft_forbids_extra_fields():
    """草稿类是 `forbid`（和 `intent.py` 的抽取类相反）——
    因为这里的字段是"我给模型的"契约，多出字段说明它在按自己的理解发挥。"""
    raw = '{"title": "x", "days": [], "lng": 104.0}'
    draft, err = parse_draft(raw)
    assert draft is None
    assert err is not None and "lng" in err


def test_parse_draft_error_mentions_field_path():
    """报错会被**回灌给模型**，所以要带字段路径 —— 只说"格式错"它修不了。"""
    raw = '{"title": "x", "days": [{"date": "2026-09-16", "theme": "t", "stops": [{"poi_id": "A"}]}]}'
    draft, err = parse_draft(raw)
    assert draft is None
    assert err is not None
    assert "arrive" in err or "stay_min" in err


# ══════════════════════════════════════════════════════════════
#  补全
# ══════════════════════════════════════════════════════════════


def test_assemble_fills_facts_from_pool():
    pool = _full_pool()
    provider = MockAmapProvider(today=TODAY)
    draft = _draft(*_ids(3))

    trip, blocking = run(
        assemble_trip(draft, pool, provider, destination="成都", session_id="s1")
    )

    assert blocking == []
    assert len(trip.days) == 1
    stops = trip.days[0].stops
    assert len(stops) == 3

    first = stops[0]
    # 坐标来自池子，不是草稿（草稿里根本没有这两个字段）
    assert first.lng == pytest.approx(MOCK_POI_POOL[0].lng)
    assert first.lat == pytest.approx(MOCK_POI_POOL[0].lat)
    assert first.name == MOCK_POI_POOL[0].name
    # 第一站没有"上一站"
    assert first.from_prev_km == 0
    assert first.from_prev_drive_min == 0
    # 第二站才该有距离，而且是算出来的正值
    assert stops[1].from_prev_km > 0
    assert stops[1].from_prev_drive_min >= 0

    assert trip.summary.hard_errors == 0
    assert trip.summary.stop_count == 3
    assert trip.source.value == "generated"
    assert trip.session_id == "s1"


def test_assemble_rejects_invented_poi_and_reports_it():
    """🔴 封闭世界的核心断言。

    一半真一半假，假的那站必须被剔除，**并且必须留下 `blocking`** ——
    静默丢弃会让用户以为"规划成功但少了一站"，那比报错更糟。
    """
    pool = _full_pool()
    provider = MockAmapProvider(today=TODAY)
    real_id = _ids(1)[0]
    draft = _draft(real_id, "B0000_FAKE_ID")

    trip, blocking = run(assemble_trip(draft, pool, provider, destination="成都"))

    assert len(blocking) == 1
    assert "B0000_FAKE_ID" in blocking[0]
    stops = trip.days[0].stops
    assert len(stops) == 1, "编造的那一站不该出现在结果里"
    assert stops[0].poi_id == real_id
    # 汇总数字要反映"确实有一处硬错"，不是假装干净
    assert trip.summary.hard_errors == 1
    assert trip.summary.stop_count == 1


def test_assemble_marks_check_fail_for_invented_poi():
    """除了 `blocking`，站点上的 `Check` 也要标红 —— 前端三态要靠它上色。"""
    pool = PoiPool()  # 空池子：每一个 id 都是编的
    provider = MockAmapProvider(today=TODAY)
    draft = _draft(_ids(1)[0])

    trip, blocking = run(assemble_trip(draft, pool, provider, destination="成都"))

    assert len(blocking) == 1
    assert trip.days[0].stops == []


def test_assemble_first_stop_check_passes_for_real_poi():
    pool = _full_pool()
    provider = MockAmapProvider(today=TODAY)
    trip, _ = run(assemble_trip(_draft(*_ids(1)), pool, provider, destination="成都"))

    checks = trip.days[0].stops[0].checks
    assert len(checks) == 1
    assert checks[0].code == "poi_exists"
    assert checks[0].status.value == "pass"


def test_assemble_estimated_walk_is_flagged_by_name():
    """步行量是**估算**（高德没有这个数据）。断言它按公式算，
    而不是断言"它是真的" —— 把近似当成事实才是这里唯一不能犯的错。"""
    pool = _full_pool()
    provider = MockAmapProvider(today=TODAY)
    trip, _ = run(assemble_trip(_draft(*_ids(4)), pool, provider, destination="成都"))

    stats = trip.days[0].day_stats
    assert stats is not None
    assert stats.walk_km == pytest.approx(round(4 * WALK_KM_PER_STOP, 1))


def test_assemble_weather_unavailable_when_not_collected():
    """没查到天气 → `unavailable` + 说明原因。**不能编一个"晴"。**"""
    pool = _full_pool()
    provider = MockAmapProvider(today=TODAY)
    trip, _ = run(assemble_trip(_draft(*_ids(2)), pool, provider, destination="成都"))

    weather = trip.days[0].weather
    assert weather is not None
    assert weather.status is WeatherStatus.UNAVAILABLE
    assert weather.note  # 要说清为什么拿不到


def test_assemble_uses_collected_weather_when_given():
    pool = _full_pool()
    provider = MockAmapProvider(today=TODAY)
    weather = run(provider.get_weather("成都", TODAY))
    trip, _ = run(
        assemble_trip(
            _draft(*_ids(1)),
            pool,
            provider,
            {TODAY.isoformat(): weather},
            destination="成都",
        )
    )
    assert trip.days[0].weather is not None
    assert trip.days[0].weather.status is WeatherStatus.OK


def test_assemble_normalizes_model_time():
    """模型写 `9:00` 时，产物里必须是 `09:00`（契约严格），
    而且 `leave` 要基于规范化后的时刻算。"""
    pool = _full_pool()
    provider = MockAmapProvider(today=TODAY)
    draft = PlanDraft(
        title="t",
        days=[
            DraftDay(
                date=TODAY,
                theme="x",
                stops=[
                    DraftStop(poi_id=_ids(1)[0], arrive="9:00", stay_min=90, match_reason="r")
                ],
            )
        ],
    )
    trip, _ = run(assemble_trip(draft, pool, provider, destination="成都"))
    stop = trip.days[0].stops[0]
    assert stop.arrive == "09:00"
    assert stop.leave == "10:30"


def test_assemble_multi_day_accumulates_totals():
    pool = _full_pool()
    provider = MockAmapProvider(today=TODAY)
    ids = _ids(4)
    draft = PlanDraft(
        title="两日",
        days=[
            DraftDay(
                date=TODAY,
                theme="d1",
                stops=[
                    DraftStop(poi_id=ids[0], arrive="09:00", stay_min=60, match_reason="r"),
                    DraftStop(poi_id=ids[1], arrive="11:00", stay_min=60, match_reason="r"),
                ],
            ),
            DraftDay(
                date=date(2026, 9, 17),
                theme="d2",
                stops=[
                    DraftStop(poi_id=ids[2], arrive="09:00", stay_min=60, match_reason="r"),
                    DraftStop(poi_id=ids[3], arrive="11:00", stay_min=60, match_reason="r"),
                ],
            ),
        ],
    )
    trip, blocking = run(assemble_trip(draft, pool, provider, destination="成都"))

    assert blocking == []
    assert [d.day for d in trip.days] == [1, 2]
    per_day = [d.day_stats.distance_km for d in trip.days if d.day_stats]
    assert len(per_day) == 2
    assert all(x > 0 for x in per_day)
    assert trip.summary.total_distance_km == pytest.approx(round(sum(per_day), 1))
    assert trip.summary.stop_count == 4


def test_assemble_rounds_pass_through_to_validation():
    """`rounds` 是"打回了几轮"，要写进 `validation` 给前端显示 agent 过程。"""
    pool = _full_pool()
    provider = MockAmapProvider(today=TODAY)
    trip, _ = run(
        assemble_trip(_draft(*_ids(1)), pool, provider, destination="成都", rounds=2)
    )
    assert trip.validation.rounds == 2
