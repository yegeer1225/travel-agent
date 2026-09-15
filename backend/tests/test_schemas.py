"""数据契约的自检 —— 只测纯逻辑，不测 LLM（符合技术方案「测试」一栏的约定）。

这组测试的价值：**契约一旦被改坏，立刻红**。
尤其是汇总数字与明细不一致这种「不报错、只显示错」的问题，只有测试能抓住。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas import CheckStatus, Trip, TripSource

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "docs" / "sample_trip.json"


@pytest.fixture(scope="module")
def sample() -> Trip:
    return Trip.model_validate(json.loads(SAMPLE.read_text(encoding="utf-8")))


# ────────────────────────────────────────────────
#  1. 样例本身必须是合法的
# ────────────────────────────────────────────────


def test_sample_trip_is_valid(sample: Trip) -> None:
    assert sample.source is TripSource.GENERATED
    assert len(sample.days) == 3
    assert sample.destination == "成都"


def test_every_stop_has_real_poi_id(sample: Trip) -> None:
    """每个站都必须是高德真实 POI —— 这是「行程不是编的」的唯一证据。"""
    for day in sample.days:
        for stop in day.stops:
            assert stop.poi_id, f"{stop.name} 缺 poi_id"
            assert stop.poi_id.startswith(("B0", "B00")), f"{stop.poi_id} 不像高德 POI ID"
            # 坐标必须在成都附近（GCJ-02），能抓到「模型编了个外地的点」
            assert 102.9 < stop.lng < 104.6, f"{stop.name} 经度跑出成都范围"
            assert 30.3 < stop.lat < 31.3, f"{stop.name} 纬度跑出成都范围"


def test_stop_seq_is_continuous(sample: Trip) -> None:
    for day in sample.days:
        assert [s.seq for s in day.stops] == list(range(1, len(day.stops) + 1))


def test_first_stop_of_day_has_zero_distance(sample: Trip) -> None:
    """当天第 1 站没有「上一站」，必须是 0（否则前端会显示一个假的里程）。"""
    for day in sample.days:
        assert day.stops[0].from_prev_km == 0
        assert day.stops[0].from_prev_drive_min == 0


# ────────────────────────────────────────────────
#  2. 汇总与明细必须一致（最容易出「静默错」的地方）
# ────────────────────────────────────────────────


def test_summary_distance_equals_sum_of_days(sample: Trip) -> None:
    total = round(sum(d.day_stats.distance_km for d in sample.days), 1)
    assert sample.summary.total_distance_km == total


def test_summary_stop_count_equals_actual(sample: Trip) -> None:
    assert sample.summary.stop_count == sum(len(d.stops) for d in sample.days)


def test_summary_error_counts_match_checks(sample: Trip) -> None:
    """hard_errors / soft_warnings 必须能从 checks 里数出来。"""
    hard_fail = soft_fail = 0
    for day in sample.days:
        for stop in day.stops:
            for c in stop.checks:
                if c.status is not CheckStatus.FAILED:
                    continue
                if c.level.value == "hard":
                    hard_fail += 1
                else:
                    soft_fail += 1
    assert sample.summary.hard_errors == hard_fail
    assert sample.summary.soft_warnings == soft_fail


def test_hard_failed_implies_not_deliverable(sample: Trip) -> None:
    """⛔ 硬判据有 fail 的行程不该被交付。样例必须是「可交付」状态。"""
    for day in sample.days:
        for stop in day.stops:
            for c in stop.checks:
                assert not (c.level.value == "hard" and c.status is CheckStatus.FAILED), (
                    f"{stop.name} 有未修复的硬错 {c.code}，不应作为成品样例"
                )


# ────────────────────────────────────────────────
#  3. 三态与「不编数据」的规则
# ────────────────────────────────────────────────


def test_check_status_is_three_valued(sample: Trip) -> None:
    allowed = {"pass", "fail", "unknown"}
    for day in sample.days:
        for stop in day.stops:
            for c in stop.checks:
                assert c.status.value in allowed


def test_unknown_checks_must_explain(sample: Trip) -> None:
    """判不了就必须说清为什么（否则用户看到的是一个没有信息量的灰点）。"""
    for day in sample.days:
        for stop in day.stops:
            for c in stop.checks:
                if c.status is CheckStatus.UNKNOWN:
                    assert c.msg, f"{stop.name} 的 {c.code} 标了 unknown 却没写原因"


def test_missing_high_de_data_stays_none_not_zero(sample: Trip) -> None:
    """高德给不了的字段 → None，**绝不允许用 0 或空字符串冒充**。"""
    for day in sample.days:
        for stop in day.stops:
            assert stop.cost_per_person is None or stop.cost_per_person > 0
    # 门票数据整体缺失时，人均总花费必须是 None 而不是 0
    assert sample.summary.total_cost_per_person is None


def test_weather_unavailable_does_not_carry_fake_numbers() -> None:
    """出发日超出预报窗口时，温度必须是 None —— 不能留一个旧值。"""
    trip = Trip.model_validate(
        {
            "trip_id": "t1",
            "session_id": None,
            "title": "x",
            "destination": "成都",
            "source": "pasted",
            "created_at": "2026-09-15T20:45:00+08:00",
            "updated_at": "2026-09-15T20:45:00+08:00",
            "days": [
                {
                    "day": 1,
                    "date": "2026-10-01",
                    "weather": {
                        "status": "unavailable",
                        "note": "出发日距今超过 4 天，无预报数据",
                    },
                    "stops": [],
                }
            ],
            "summary": {
                "total_distance_km": 0,
                "total_cost_per_person": None,
                "stop_count": 0,
                "hard_errors": 0,
                "soft_warnings": 0,
            },
        }
    )
    w = trip.days[0].weather
    assert w is not None and w.status.value == "unavailable"
    assert w.day_temp is None
    assert w.note


# ────────────────────────────────────────────────
#  4. 契约的护栏：这三条挂了说明有人在偷偷改契约
# ────────────────────────────────────────────────


def test_pasted_trip_must_have_null_session(sample: Trip) -> None:
    """A37：粘贴来的行程没有会话。**这条在 graph/store 层也要再查一遍。**"""
    pasted = sample.model_copy(update={"source": TripSource.PASTED, "session_id": None})
    assert pasted.session_id is None
    # 反向：生成态必须有 session_id（用于「继续聊改」）
    assert sample.session_id is not None


def test_illegal_time_format_is_rejected(sample: Trip) -> None:
    """时间必须是 HH:MM —— 抓「9:00」「09:00:00」「早上9点」这类写法。"""
    d = sample.model_dump(mode="json")
    d["days"][0]["stops"][0]["arrive"] = "9:00"
    with pytest.raises(ValidationError):
        Trip.model_validate(d)


def test_unknown_field_is_rejected(sample: Trip) -> None:
    """extra=forbid：谁往里加没定义的字段，立刻报错 —— 防契约漂移。"""
    d = sample.model_dump(mode="json")
    d["hotness"] = "10.0"  # 参考设计里那个高德根本不存在的字段
    with pytest.raises(ValidationError):
        Trip.model_validate(d)
