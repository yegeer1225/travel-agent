"""M7 确定性重算测试：op 应用（结构）+ 数值重算。provider 全 mock，不连 LLM。

PATCH 的两个易错点在这里钉死：
1. **move 是插入不是交换**（api.md 3.2）—— 交换会让两站时间串位
2. **判据重算必须幂等**（先清空再重填）—— 不清的话重算两次判据翻倍
"""

from __future__ import annotations

from datetime import date

import pytest

from conftest import NOW, make_trip
from fakes import run

from app.graph.intent import Requirements
from app.graph.recompute import OpApplyError, apply_ops, pool_from_trip, recompute_trip
from app.providers.mock import MOCK_POI_POOL, MockAmapProvider
from app.schemas import Day, Stop, Trip, WeatherStatus
from app.tools.poi_pool import PoiPool

TODAY = date(2026, 9, 16)
BY_NAME = {p.name: p for p in MOCK_POI_POOL}


def _stop(seq: int, poi_name: str, *, arrive: str, stay: int = 90, prev_km: float = 0.0) -> Stop:
    poi = BY_NAME[poi_name]
    return Stop(
        seq=seq, name=poi.name, poi_id=poi.poi_id, lng=poi.lng, lat=poi.lat,
        arrive=arrive, stay_min=stay, leave=None,
        from_prev_km=prev_km, from_prev_drive_min=0,
    )


def make_full_trip() -> Trip:
    """两天各两站、带日期与时间的完整行程（重算测试的固定输入）。"""
    base = make_trip("t-1")
    days = [
        Day(day=1, date=date(2026, 9, 20), stops=[
            _stop(1, "成都武侯祠博物馆", arrive="09:00"),
            _stop(2, "锦里古街", arrive="11:30"),
        ]),
        Day(day=2, date=date(2026, 9, 21), stops=[
            _stop(1, "宽窄巷子景区", arrive="09:30"),
            _stop(2, "人民公园", arrive="11:00"),
        ]),
    ]
    return base.model_copy(update={"days": days})


def _req() -> Requirements:
    return Requirements().with_defaults()


# ══════════════════════════════════════════════════════════════
#  apply_ops —— 结构
# ══════════════════════════════════════════════════════════════


def test_move_within_day_reorders():
    trip = make_full_trip()
    _, pinned = run(_apply(trip, [{"op": "move", "day": 1, "seq": 2, "to_seq": 1}]))
    names = [s.name for s in trip.days[0].stops]
    assert names == ["锦里古街", "成都武侯祠博物馆"]
    assert [s.seq for s in trip.days[0].stops] == [1, 2], "seq 必须重编"
    assert pinned == {} and not _crossed(pinned)


async def _apply(trip, ops):
    from app.schemas import PatchTripRequest, TripOp

    req = PatchTripRequest(ops=[TripOp.model_validate(o) for o in ops])
    return apply_ops(trip, req.ops)


def _crossed(pinned) -> bool:  # pragma: no cover —— 占位可读性辅助
    return False


def test_move_across_days_inserts_not_swaps():
    """跨天 move：插入语义 —— 目标天原站**顺延**，不是交换（D44）。"""
    trip = make_full_trip()
    weather_days, _ = run(_apply(trip, [{"op": "move", "day": 1, "seq": 2, "to_day": 2, "to_seq": 1}]))

    assert [s.name for s in trip.days[1].stops] == [
        "锦里古街", "宽窄巷子景区", "人民公园",
    ], "被插入的站成为目标天的新首站"
    assert [s.seq for s in trip.days[1].stops] == [1, 2, 3]
    assert [s.name for s in trip.days[0].stops] == ["成都武侯祠博物馆"]
    assert weather_days == {1, 2}, "跨天进/出两天的天气要重查"


def test_delete_stop():
    trip = make_full_trip()
    run(_apply(trip, [{"op": "delete", "day": 1, "seq": 1}]))
    assert [s.name for s in trip.days[0].stops] == ["锦里古街"]
    assert trip.days[0].stops[0].seq == 1


def test_update_time_sets_values():
    trip = make_full_trip()
    _, pinned = run(_apply(trip, [{"op": "update_time", "day": 1, "seq": 1, "arrive": "10:30", "stay_min": 60}]))
    assert trip.days[0].stops[0].arrive == "10:30"
    assert trip.days[0].stops[0].stay_min == 60
    assert pinned == {trip.days[0].stops[0].poi_id: "10:30"}, "锚点按 poi_id 记录"


def test_pinned_arrive_survives_same_batch_move():
    """同批 update_time + move：锚点按 poi_id 寻址，seq 重排不丢锚。

    场景：给第 1 天第 2 站设 12:00，同批又把它跨天挪到第 2 天的中间位 ——
    旧实现按 (day, seq) 寻址，move 重排后键位指不到这站，显式时间被顺推悄悄覆盖。
    """
    trip = make_full_trip()
    provider = MockAmapProvider(today=TODAY)
    weather_days, pinned = run(_apply(trip, [
        {"op": "update_time", "day": 1, "seq": 2, "arrive": "12:00"},
        {"op": "move", "day": 1, "seq": 2, "to_day": 2, "to_seq": 2},  # 挪到第 2 天中间位
    ]))
    moved = trip.days[1].stops[1]
    assert moved.name == "锦里古街" and moved.poi_id in pinned
    run(recompute_trip(trip, provider=provider, requirements=_req(),
                       weather_days=weather_days, pinned_arrive=pinned))
    assert moved.arrive == "12:00", "pinned 时间赢了顺推重排"


def test_bad_ops_are_rejected_wholesale():
    trip = make_full_trip()
    with pytest.raises(OpApplyError):
        run(_apply(trip, [{"op": "delete", "day": 1, "seq": 9}]))
    with pytest.raises(OpApplyError):
        run(_apply(trip, [{"op": "move", "day": 5, "seq": 1, "to_seq": 1}]))
    with pytest.raises(OpApplyError):
        run(_apply(trip, [{"op": "update_time", "day": 1, "seq": 1}]))  # 两个值都没给


# ══════════════════════════════════════════════════════════════
#  recompute_trip —— 数值
# ══════════════════════════════════════════════════════════════


def test_recompute_timeline_and_summary():
    trip = make_full_trip()
    provider = MockAmapProvider(today=TODAY)
    weather_days, pinned = run(_apply(trip, [{"op": "move", "day": 1, "seq": 2, "to_seq": 1}]))
    report = run(recompute_trip(trip, provider=provider, requirements=_req(),
                                weather_days=weather_days, pinned_arrive=pinned))

    day1 = trip.days[0]
    first, second = day1.stops
    assert first.from_prev_km == 0 and first.from_prev_drive_min == 0, "首站 from_prev=0（D43 口径）"
    assert second.from_prev_km > 0 and second.from_prev_drive_min >= 0, "第二站距离由 provider 算出"
    # 时间轴顺推：arrive[2] = leave[1] + drive[2]
    h1, m1 = map(int, first.leave.split(":"))
    h2, m2 = map(int, second.arrive.split(":"))
    drive = second.from_prev_drive_min
    assert (h2 * 60 + m2) - (h1 * 60 + m1) == drive
    assert day1.day_stats.distance_km == round(first.from_prev_km + second.from_prev_km, 1)
    assert day1.day_stats.walk_km == round(2 * 1.2, 1)
    assert trip.summary.stop_count == 4
    assert trip.summary.total_distance_km == round(
        sum(d.day_stats.distance_km for d in trip.days), 1
    )
    assert report.passed, "池子里的真 POI、合理车程 —— 不该有硬错"


def test_recompute_is_idempotent():
    """判据重算幂等：跑两遍 checks 不翻倍、数值稳定（重算路径专属的坑）。"""
    trip = make_full_trip()
    provider = MockAmapProvider(today=TODAY)
    run(recompute_trip(trip, provider=provider, requirements=_req()))
    first_checks = sum(len(d.checks) + sum(len(s.checks) for s in d.stops) for d in trip.days)
    first_summary = trip.summary.model_dump(mode="json")
    run(recompute_trip(trip, provider=provider, requirements=_req()))
    second_checks = sum(len(d.checks) + sum(len(s.checks) for s in d.stops) for d in trip.days)
    assert first_checks == second_checks > 0, "判据数量稳定 = 幂等"
    assert trip.summary.model_dump(mode="json") == first_summary


def test_recompute_pinned_arrive_wins():
    """update_time 的 arrive 是显式锚点：顺推以它为准。"""
    trip = make_full_trip()
    provider = MockAmapProvider(today=TODAY)
    weather_days, pinned = run(_apply(trip, [{"op": "update_time", "day": 1, "seq": 1, "arrive": "13:00", "stay_min": 30}]))
    run(recompute_trip(trip, provider=provider, requirements=_req(),
                       weather_days=weather_days, pinned_arrive=pinned))
    first = trip.days[0].stops[0]
    assert first.arrive == "13:00" and first.leave == "13:30"
    h1, m1 = map(int, first.leave.split(":"))
    second = trip.days[0].stops[1]
    h2, m2 = map(int, second.arrive.split(":"))
    assert (h2 * 60 + m2) - (h1 * 60 + m1) == second.from_prev_drive_min


def test_cross_day_weather_requeried():
    """跨天 move 后：有日期的天重查（超窗 → unavailable 如实说）；无日期 → 置 unavailable。"""
    trip = make_full_trip()
    provider = MockAmapProvider(today=TODAY)
    run(recompute_trip(trip, provider=provider, requirements=_req(), weather_days={1, 2}))
    for day in trip.days:
        assert day.weather is not None
        # 09-20/21 都在 4 天窗口（16~19）外 → mock 如实返回不可用
        assert day.weather.status is WeatherStatus.UNAVAILABLE

    trip2 = make_full_trip()
    for day in trip2.days:
        day.date = None
    run(recompute_trip(trip2, provider=provider, requirements=_req(), weather_days={1}))
    assert "没有明确日期" in (trip2.days[0].weather.note or "")


def test_pool_from_trip_makes_poi_exists_pass():
    """行程自建池：生成/粘贴时已过闸门的站，PATCH 重算后 poi_exists 恒 pass 是预期。"""
    trip = make_full_trip()
    pool = run(_pool(trip))
    assert pool.contains(BY_NAME["成都武侯祠博物馆"].poi_id)

    from app.graph.validate import check_poi_exists

    assert check_poi_exists(trip.days[0].stops[0].poi_id, pool).status.value == "pass"


async def _pool(trip):
    return pool_from_trip(trip)
