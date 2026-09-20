"""校验体系自检 —— **项目的核心那一块**（内部需求文档 6.1/6.2）。

═══════════════════════════════════════════════════════════════
 这批测试真正要守住的三件事（不是"函数能跑"）
═══════════════════════════════════════════════════════════════

1. **`unknown` 不许退化成 `pass`**
   这是三态存在的全部意义。数据拿不到时返回 pass 是**撒谎**，
   返回 fail 是**误打回**（agent 白烧几轮）。每一条会落 `unknown` 的判据
   都必须有一个测试**专门逼出那个 `unknown`** —— 否则这条路径永远没人跑过。

2. **`unknown` 不许进 `blocking`**
   三态写了但不影响流程，等于没写。`blocking` 是喂给 `repair` 的打回理由，
   混进一条"判不了"会让 agent 去修一个**根本不存在的问题**。

3. **假阳性比假阴性贵**
   判据多报一个错 → 打回重排 → 又一次 LLM 生成（钱 + 十几秒 + 用户等待）。
   少报一个 → 只是少提醒一句。
   → 所以每一条阈值判据都要有"**刚好在界内**"的测试，
     和"**刚好超一点点就红**"的测试，两边都钉住。

═══════════════════════════════════════════════════════════════
 为什么不打真高德、不打真 LLM
═══════════════════════════════════════════════════════════════

`validate_trip` 的 `distance_fn` 是可注入的（见 `validate.py` 的类型别名注释），
所以跨天段可以塞一个"永远返回 200 分钟"的假函数 —— 不起网络、不花钱、完全确定。
天气直接用 `Weather(...)` 造，POI 池直接用 `MOCK_POI_POOL`（里面 `type` 是真值，
`is_outdoor` 那条判据才有东西可判）。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

from app.graph.intent import Requirements, Travelers
from app.graph.validate import (
    MAX_DAY_DRIVE_MIN,
    MAX_DAY_DRIVE_MIN_WITH_ELDERS,
    MAX_HOP_DRIVE_MIN,
    MAX_HOP_DRIVE_MIN_WITH_ELDERS,
    MAX_WALK_KM,
    MAX_WALK_KM_WITH_CHILDREN,
    MAX_WALK_KM_WITH_ELDERS,
    check_cross_day_hop,
    check_day_drive,
    check_hop_drive,
    check_open_today,
    check_poi_exists,
    check_walk_load,
    check_weather_conflict,
    is_outdoor,
    parse_open_windows,
    validate_trip,
)
from app.providers.base import DistanceResult
from app.providers.mock import MOCK_POI_POOL, MockAmapProvider
from app.schemas import (
    Check,
    CheckLevel,
    CheckStatus,
    Day,
    DayStats,
    Stop,
    Trip,
    TripSource,
    TripSummary,
    Weather,
    WeatherStatus,
)
from app.tools.poi_pool import PoiPool

TODAY = date(2026, 9, 16)


def run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════
#  造数据的小工具
# ══════════════════════════════════════════════════════════════

# 池子里的锚点（按 mock 池顺序，写 id 比写下标好读）
WUHOU = "B001C07VJ2"  # 武侯祠博物馆 —— type 是「科教文化服务;博物馆;博物馆」= 室内
JINLI = "B0FFFD3P2C"  # 锦里古街 —— 「风景名胜;风景名胜;风景名胜」= 户外
QINGCHENG = "B001C06ESL"  # 青城山 —— 实测**没有** open_time，是三态的真实来源


def _pool(*poi_ids: str) -> PoiPool:
    """从 mock 池里挑几个建候选池。不传 = 全量。"""
    pool = PoiPool()
    wanted = set(poi_ids)
    pool.record([p for p in MOCK_POI_POOL if not wanted or p.poi_id in wanted])
    return pool


def _poi(poi_id: str):
    return next(p for p in MOCK_POI_POOL if p.poi_id == poi_id)


def stop(
    poi_id: str = WUHOU,
    *,
    seq: int = 1,
    arrive: str | None = "10:00",
    open_time: str | None = "08:30-18:30",
    from_prev_drive_min: int = 0,
) -> Stop:
    """造一站。坐标从池子里取真值（否则 `check_cross_day_hop` 拿不到经纬度）。"""
    p = _poi(poi_id)
    return Stop(
        seq=seq,
        name=p.name,
        poi_id=poi_id,
        lng=p.lng,
        lat=p.lat,
        arrive=arrive,
        stay_min=90,
        from_prev_drive_min=from_prev_drive_min,
        open_time=open_time,
    )


def invented_stop(poi_id: str = "B0000FAKE", **kw) -> Stop:
    """造一站**编造的 poi_id**（池子里没有）。

    坐标借武侯祠的 —— 这不是偷懒：编造的 id 本来就没有真坐标，
    而 `poi_exists` 判据**只看 id**，坐标不参与判定。
    借一个真点的坐标，反而能保证"这条判据红了是因为 id，不是因为坐标是 0"。
    """
    real = _poi(WUHOU)
    base = dict(seq=1, arrive="10:00", stay_min=90)
    base.update(kw)
    return Stop(
        name="编造的地点",
        poi_id=poi_id,
        lng=real.lng,
        lat=real.lat,
        open_time=None,
        **base,
    )


def day(number: int = 1, stops: list[Stop] | None = None, **kw) -> Day:
    return Day(day=number, date=TODAY, stops=stops or [], **kw)


def trip(days: list[Day]) -> Trip:
    return Trip(
        trip_id="t1",
        title="测试行程",
        destination="成都",
        source=TripSource.GENERATED,
        created_at=datetime(2026, 9, 16, 10, 0),
        updated_at=datetime(2026, 9, 16, 10, 0),
        days=days,
        summary=TripSummary(total_distance_km=0, stop_count=0, hard_errors=0, soft_warnings=0),
    )


def check_status(checks: list[Check], code: str) -> list[CheckStatus]:
    """取某个 code 的全部状态。**用 list 而不是单值**：
    `reachable` 在一天里会出现多次（每站一个 + 跨天段 + 当天累计），
    只取一个会让"漏了一条"这种 bug 看不见。"""
    return [c.status for c in checks if c.code == code]


def always(minutes: int, km: float = 40.0):
    """假距离函数 —— 永远返回同一个耗时。用来精确控制跨天段。"""

    async def _fn(origin, dest) -> DistanceResult:
        return DistanceResult(km=km, drive_min=minutes, straight_km=km / 1.2)

    return _fn


def boom():
    """假距离函数 —— 永远抛异常。用来逼出跨天段的 `unknown`。"""

    async def _fn(origin, dest) -> DistanceResult:
        raise TimeoutError("模拟高德超时")

    return _fn


# ══════════════════════════════════════════════════════════════
#  一、营业时间解析 —— 判据的输入，解析错了判据全错
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("08:30-18:30", [(510, 1110)]),
        # 实测真值：旺季/淡季两套时间挤在一个字段里（熊猫基地就是这样）
        ("07:30-17:00 08:00-16:30", [(450, 1020), (480, 990)]),
        ("24小时营业", [(0, 1439)]),
        ("全天开放", [(0, 1439)]),
        # 跨夜（餐饮常见）：止 < 起，**不许**当成"结束时间早于开始 = 数据坏了"
        ("11:00-02:00", [(660, 120)]),
        ("14:00~22:30", [(840, 1350)]),  # 波浪号也算分隔符
        ("08:00 至 18:00", [(480, 1080)]),  # 中文"至"
    ],
)
def test_parse_open_windows_known_forms(raw: str, expected: list[tuple[int, int]]) -> None:
    assert parse_open_windows(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "营业时间待更新", "见门口公告"])
def test_parse_open_windows_returns_none_for_unknown(raw: str | None) -> None:
    """🔴 认不出来就返回 `None`，**不要去猜**。

    猜的后果：把"待更新"猜成"整天营业" → 判据绿了 → 用户在闭馆日白跑一趟。
    返回 `None` 则让判据落 `unknown`（灰色"无法判定"），这是诚实的。
    """
    assert parse_open_windows(raw) is None


def test_parse_open_windows_rejects_impossible_times() -> None:
    """形似但不是时间的值要挡掉（`1:99` 这种）。

    解析器只认 `HH:MM` 结构，所以 `25:00` 这类越界值必须被丢 ——
    丢完没有合法区间就返回 None（= 判不了），**不是**返回 `[(1500, x)]`
    让一个荒唐的时间进入判据。
    """
    assert parse_open_windows("25:00-26:00") is None
    assert parse_open_windows("08:99-18:00") is None


# ══════════════════════════════════════════════════════════════
#  二、户外判定 —— 用池子里的**实测** type
# ══════════════════════════════════════════════════════════════


def test_is_outdoor_uses_measured_type() -> None:
    """武侯祠是「博物馆」→ 室内；青城山是「风景名胜」→ 户外。

    ⚠️ 这两个 `type` 是 2026-09-15 实测补采的，不是编的 ——
       如果它们是编的，这条测试就是在验证一个幻觉。
    """
    assert is_outdoor(_poi(WUHOU)) is False, "博物馆不该算户外"
    assert is_outdoor(_poi(QINGCHENG)) is True, "风景名胜该算户外"
    assert is_outdoor(_poi(JINLI)) is True


def test_is_outdoor_unknown_type_is_not_outdoor() -> None:
    """🔴 判不了就当**不是**户外（宁可漏判不误判）。

    反过来的代价：手写/粘贴来的行程没有 `type` → 全被当成户外 →
    一下雨整份行程被暴雨判据打回重排。**假阳性比假阴性贵。**
    """
    assert is_outdoor(None) is False
    loose = _poi(WUHOU).model_copy(update={"type": None})
    assert is_outdoor(loose) is False


# ══════════════════════════════════════════════════════════════
#  三、① poi_exists —— 唯一不会落 unknown 的判据
# ══════════════════════════════════════════════════════════════


def test_poi_exists_passes_for_pool_member() -> None:
    assert check_poi_exists(WUHOU, _pool()).status is CheckStatus.PASSED


def test_poi_exists_fails_for_invented_id() -> None:
    """模型编的 id → **硬错**。这是整条可信度链路的锚点。"""
    c = check_poi_exists("B0000FAKE", _pool())
    assert c.status is CheckStatus.FAILED
    assert c.level is CheckLevel.HARD
    assert c.msg and "候选池" in c.msg


def test_poi_exists_never_returns_unknown() -> None:
    """有池子就一定判得了 —— 所以它**不该**有 `unknown` 分支。

    这条是反向断言：如果哪天有人给它加了 `unknown`，说明引入了一个
    "池子是空的"这种可以静默失效的状态，而那正是 `poi_pool.py` 里
    `current_pool()` 返回 `None` 而不是造一个空池子所防的事。
    """
    for poi_id in [WUHOU, "B0000FAKE", "", "???"]:
        assert check_poi_exists(poi_id, _pool()).status in {
            CheckStatus.PASSED,
            CheckStatus.FAILED,
        }


# ══════════════════════════════════════════════════════════════
#  四、② open_today —— 会落 unknown 的第一条
# ══════════════════════════════════════════════════════════════


def test_open_today_passes_when_arrive_inside_window() -> None:
    c = check_open_today(stop(arrive="10:00", open_time="08:30-18:30"))
    assert c.status is CheckStatus.PASSED


def test_open_today_fails_when_arrive_too_late() -> None:
    c = check_open_today(stop(arrive="19:00", open_time="08:30-18:30"))
    assert c.status is CheckStatus.FAILED
    assert c.msg and "19:00" in c.msg and "08:30-18:30" in c.msg


def test_open_today_boundary_is_inclusive() -> None:
    """刚好卡在开门/关门那一分钟算**通过**。

    边界写成 `<` 而不是 `<=` 会让"09:00 到、09:00 开门"被判成硬错 ——
    用户会觉得这个工具在找茬。边界宽松一点不会有实际损失。
    """
    assert check_open_today(stop(arrive="08:30", open_time="08:30-18:30")).status is (
        CheckStatus.PASSED
    )
    assert check_open_today(stop(arrive="18:30", open_time="08:30-18:30")).status is (
        CheckStatus.PASSED
    )


def test_open_today_overnight_window() -> None:
    """跨夜营业（`11:00-02:00`）：23:00 到算通过，10:00 到算没过。"""
    assert check_open_today(stop(arrive="23:00", open_time="11:00-02:00")).status is (
        CheckStatus.PASSED
    )
    assert check_open_today(stop(arrive="01:00", open_time="11:00-02:00")).status is (
        CheckStatus.PASSED
    )
    assert check_open_today(stop(arrive="10:00", open_time="11:00-02:00")).status is (
        CheckStatus.FAILED
    )


def test_open_today_unknown_when_no_open_time() -> None:
    """🔴 **三态的真实来源**：高德确实没返回青城山的营业时间。

    这里必须落 `unknown` ——
    · 落 `pass` = 撒谎（"确认它开门了"，而我们根本不知道）
    · 落 `fail` = 误打回（把青城山从行程里删掉，而它其实正常营业）
    """
    c = check_open_today(stop(QINGCHENG, open_time=None))
    assert c.status is CheckStatus.UNKNOWN
    assert c.msg and "判不了" in c.msg


def test_open_today_unknown_when_format_unrecognized() -> None:
    c = check_open_today(stop(open_time="详情请咨询景区"))
    assert c.status is CheckStatus.UNKNOWN


def test_open_today_unknown_when_no_arrive() -> None:
    """粘贴来的行程经常没有到达时刻 —— 这时也判不了，不是"通过"。"""
    c = check_open_today(stop(arrive=None))
    assert c.status is CheckStatus.UNKNOWN


# ══════════════════════════════════════════════════════════════
#  五、③ reachable —— 单跳 / 当天累计 / 跨天段
# ══════════════════════════════════════════════════════════════


def test_hop_drive_boundary() -> None:
    """刚好等于上限算通过，超 1 分钟算失败。"""
    at_limit = check_hop_drive(
        stop(from_prev_drive_min=MAX_HOP_DRIVE_MIN), "上一站", Travelers(adults=2)
    )
    over = check_hop_drive(
        stop(from_prev_drive_min=MAX_HOP_DRIVE_MIN + 1), "上一站", Travelers(adults=2)
    )
    assert at_limit.status is CheckStatus.PASSED
    assert over.status is CheckStatus.FAILED


def test_hop_drive_threshold_tightens_with_elders() -> None:
    """🔴 同一个车程，带长辈时该红、不带长辈时该绿。

    这是"用户画了 `Travelers` 三个字段"这件事**唯一被真正用到**的地方 ——
    阈值是一个数的话，这条判据对两类用户必然有一类是错的。
    """
    minutes = (MAX_HOP_DRIVE_MIN_WITH_ELDERS + MAX_HOP_DRIVE_MIN) // 2  # 105
    s = stop(from_prev_drive_min=minutes)

    young = check_hop_drive(s, "上一站", Travelers(adults=2))
    with_elders = check_hop_drive(s, "上一站", Travelers(adults=2, elders=2))

    assert young.status is CheckStatus.PASSED, f"{minutes} 分钟对年轻人应该没问题"
    assert with_elders.status is CheckStatus.FAILED
    assert with_elders.msg and "长辈" in with_elders.msg, "理由要说清是因为带了长辈才收紧"


def test_day_drive_threshold_tightens_with_elders() -> None:
    minutes = (MAX_DAY_DRIVE_MIN_WITH_ELDERS + MAX_DAY_DRIVE_MIN) // 2  # 165
    assert check_day_drive(minutes, Travelers(adults=2)).status is CheckStatus.PASSED
    assert check_day_drive(minutes, Travelers(adults=2, elders=1)).status is CheckStatus.FAILED


def test_day_drive_fail_message_explains_why() -> None:
    """"单跳都不超但一天跑五段"也要能被抓到 —— msg 得说清是"累计超了"。"""
    c = check_day_drive(MAX_DAY_DRIVE_MIN + 10, Travelers(adults=1))
    assert c.status is CheckStatus.FAILED
    assert c.msg and "累计" in c.msg


@pytest.mark.parametrize(
    ("minutes", "elders", "expected"),
    [
        (MAX_HOP_DRIVE_MIN, 0, CheckStatus.PASSED),
        (MAX_HOP_DRIVE_MIN + 1, 0, CheckStatus.FAILED),
        (MAX_HOP_DRIVE_MIN_WITH_ELDERS, 1, CheckStatus.PASSED),
        (MAX_HOP_DRIVE_MIN_WITH_ELDERS + 1, 1, CheckStatus.FAILED),
    ],
)
def test_cross_day_hop_thresholds(minutes: int, elders: int, expected: CheckStatus) -> None:
    """跨天段：昨天最后一站 → 今天第一站。阈值与单跳同一条表（它也"是一跳"）。"""
    travelers = Travelers(adults=2, elders=elders) if elders else Travelers(adults=2)
    c = run(check_cross_day_hop(stop(JINLI), stop(QINGCHENG), always(minutes), travelers))
    assert c.status is expected


def test_cross_day_hop_message_names_both_ends() -> None:
    """🔴 打回理由必须**点名两端**。

    只说"车程 130 分钟超限"的话，模型不知道改哪天 —— 而它重排时手里是整份行程。
    点名"都江堰"和"青城山"它才知道该把哪一站挪走。
    """
    c = run(check_cross_day_hop(stop(JINLI), stop(QINGCHENG), always(200), Travelers(adults=1)))
    assert c.status is CheckStatus.FAILED
    assert c.msg
    assert _poi(JINLI).name in c.msg
    assert _poi(QINGCHENG).name in c.msg
    assert "200" in c.msg


def test_cross_day_hop_is_unknown_when_distance_call_fails() -> None:
    """距离接口挂了 → `unknown`，**不是 `fail`**。

    落 fail 的后果：高德一次超时就让一份可能完全正常的行程被打回重排。
    落 unknown 只是那条判据显示灰色，其余照常。
    """
    c = run(check_cross_day_hop(stop(JINLI), stop(QINGCHENG), boom(), Travelers(adults=2)))
    assert c.status is CheckStatus.UNKNOWN
    assert c.msg and "TimeoutError" in c.msg, "msg 要写清是哪种失败，便于排查"


# ══════════════════════════════════════════════════════════════
#  六、④ weather_conflict
# ══════════════════════════════════════════════════════════════


def _weather(day_w: str, night_w: str = "晴") -> Weather:
    return Weather(
        status=WeatherStatus.OK,
        day_weather=day_w,
        night_weather=night_w,
        day_temp=25,
        night_temp=18,
        report_time="2026-09-16 08:00:00",
    )


def test_weather_conflict_fails_on_storm_plus_outdoor() -> None:
    stops = [stop(QINGCHENG, open_time="08:00-18:00")]
    c = check_weather_conflict(_weather("暴雨"), stops, _pool())
    assert c.status is CheckStatus.FAILED
    assert c.msg and "暴雨" in c.msg and _poi(QINGCHENG).name in c.msg


def test_weather_conflict_passes_on_storm_plus_indoor() -> None:
    """暴雨 + 全室内（博物馆/商场）→ 通过。

    这条是判据的"另一半"：只知道看天气、不看景点类型的话，
    一份雨天排满博物馆的行程会被误判成冲突。
    """
    c = check_weather_conflict(_weather("暴雨"), [stop(WUHOU)], _pool())
    assert c.status is CheckStatus.PASSED


@pytest.mark.parametrize("mild", ["阵雨", "小雨", "雷阵雨", "多云", "阴"])
def test_weather_conflict_ignores_mild_weather(mild: str) -> None:
    """🔴 "阵雨"**不算冲突** —— 关键词表故意没收它。

    实测成都那几天的预报就是"阵雨/阴"，收进来的话**一份正常行程会被天天打回**。
    假阳性比假阴性贵：少提醒一句没事，白烧几轮重排有事。
    """
    c = check_weather_conflict(_weather(mild), [stop(QINGCHENG)], _pool())
    assert c.status is CheckStatus.PASSED


def test_weather_conflict_checks_night_weather_too() -> None:
    """夜间暴雨也算 —— 夜游锦里同样是户外。"""
    c = check_weather_conflict(_weather("多云", "大暴雨"), [stop(JINLI)], _pool())
    assert c.status is CheckStatus.FAILED


def test_weather_conflict_unknown_without_data() -> None:
    """天气没查到 → `unknown`，不是 `pass`。"""
    assert check_weather_conflict(None, [stop(QINGCHENG)], _pool()).status is CheckStatus.UNKNOWN

    unavailable = Weather(status=WeatherStatus.UNAVAILABLE, note="超出预报窗口")
    c = check_weather_conflict(unavailable, [stop(QINGCHENG)], _pool())
    assert c.status is CheckStatus.UNKNOWN
    assert c.msg and "判不了" in c.msg


# ══════════════════════════════════════════════════════════════
#  七、⑤ walk_load
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("walk_km", "travelers", "expected"),
    [
        (MAX_WALK_KM - 0.1, Travelers(adults=2), CheckStatus.PASSED),
        (MAX_WALK_KM, Travelers(adults=2), CheckStatus.PASSED),
        (MAX_WALK_KM + 0.1, Travelers(adults=2), CheckStatus.FAILED),
        # 带小孩 6km 就红
        (MAX_WALK_KM_WITH_CHILDREN + 0.1, Travelers(adults=2, children=1), CheckStatus.FAILED),
        (MAX_WALK_KM_WITH_CHILDREN - 0.1, Travelers(adults=2, children=1), CheckStatus.PASSED),
        # 带老人更严：5km 就红，而 5.5km 对普通成人还是绿的
        (MAX_WALK_KM_WITH_ELDERS + 0.5, Travelers(adults=2, elders=1), CheckStatus.FAILED),
        (MAX_WALK_KM_WITH_ELDERS + 0.5, Travelers(adults=2), CheckStatus.PASSED),
    ],
)
def test_walk_load_tiers(walk_km: float, travelers: Travelers, expected: CheckStatus) -> None:
    """三档阈值：长辈 5km < 小孩 6km < 成人 8km。老人优先（更严）。"""
    assert check_walk_load(walk_km, travelers).status is expected


def test_walk_load_message_says_estimated() -> None:
    """🔴 `walk_km` 是**估算**（站数 × 1.2km），msg 里必须写"估算"。

    高德没有"两点间步行距离"这个数据 —— 不写"估算"的话，
    用户会以为这是算出来的精确值，而这个判据其实抓不到
    "这两个站其实在同一个公园里"。
    """
    c = check_walk_load(9.0, Travelers(adults=2))
    assert c.status is CheckStatus.FAILED
    assert c.msg and "估算" in c.msg


# ══════════════════════════════════════════════════════════════
#  八、validate_trip —— 汇总与分流
# ══════════════════════════════════════════════════════════════

REQ = Requirements(destination="成都", days=2, travelers=Travelers(adults=2))


def test_validate_trip_writes_checks_into_trip() -> None:
    """判据必须**写回行程对象** —— 前端渲染的就是 `trip.days[].checks`。"""
    d = day(1, [stop(WUHOU)])
    d.day_stats = DayStats(distance_km=10, drive_min=30, walk_km=3)
    d.weather = _weather("晴")
    t = trip([d])

    report = run(validate_trip(t, _pool(), REQ))
    assert report.passed
    assert d.stops[0].checks, "站点级判据要落在 Stop 上"
    assert d.checks, "天级判据要落在 Day 上（D37）"


def test_validate_trip_is_idempotent() -> None:
    """🔴 跑两次不能让判据翻倍。

    不清空的话，同一份行程校验两次 → 每个判据出现两遍 → 前端满屏重复红标。
    而这个 bug **只在"重算"路径上出现**（首次生成看不出来）——
    也就是界面二拖拽后才暴露，属于最难在现场发现的那一类。
    """
    d = day(1, [stop(WUHOU), stop(JINLI, seq=2)])
    d.day_stats = DayStats(distance_km=10, drive_min=30, walk_km=3)
    t = trip([d])

    run(validate_trip(t, _pool(), REQ))
    first_stop = len(d.stops[0].checks)
    first_day = len(d.checks)

    run(validate_trip(t, _pool(), REQ))
    assert len(d.stops[0].checks) == first_stop
    assert len(d.checks) == first_day


def test_validate_trip_unknown_never_blocks() -> None:
    """🔴 **三态的意义所在**：整份行程全是 `unknown`，但一条 `blocking` 都不许有。

    构造：青城山没有 open_time（→ unknown）+ 天气查不到（→ unknown）。
    如果 `unknown` 混进 blocking，agent 就会去打回重排 —— 修一个不存在的问题，
    而且**永远修不好**（因为问题不在行程里，在数据里），死循环。
    """
    d = day(1, [stop(QINGCHENG, open_time=None)])
    d.day_stats = DayStats(distance_km=60, drive_min=80, walk_km=3)
    d.weather = Weather(status=WeatherStatus.UNAVAILABLE, note="超出 4 天窗口")
    t = trip([d])

    report = run(validate_trip(t, _pool(), REQ))
    assert report.unknown, "这一份应该确实产出了 unknown（否则测试本身失效了）"
    assert report.blocking == [], f"unknown 混进了 blocking：{report.blocking}"
    assert report.passed


def test_validate_trip_hard_failure_blocks() -> None:
    """编造的 poi_id → 必须进 blocking，且理由带上"第几天"。"""
    d = day(2, [invented_stop()])
    d.day_stats = DayStats(distance_km=1, drive_min=5, walk_km=1)
    t = trip([d])

    report = run(validate_trip(t, _pool(), REQ))
    assert not report.passed
    assert any("第 2 天" in reason for reason in report.blocking), (
        f"打回理由要写清是哪一天：{report.blocking}"
    )


def test_validate_trip_skips_cross_day_without_distance_fn() -> None:
    """不传 `distance_fn` 就**不打跨天段**。

    这是**有意的默认值**：跨天段要额外打一次高德接口，
    而"拖拽后重算"这种场景（只改了顺序）不该被它拖慢。
    ⚠️ 所以这个默认值的代价必须被测试记住：不传 = 漏掉远郊首站那一段。
    """
    d1 = day(1, [stop(JINLI)])
    d2 = day(2, [stop(QINGCHENG, seq=1)], )
    for d in (d1, d2):
        d.day_stats = DayStats(distance_km=5, drive_min=15, walk_km=2)
    t = trip([d1, d2])

    report = run(validate_trip(t, _pool(), REQ))  # 不传 distance_fn
    assert report.passed
    assert not d2.stops[0].checks or all(
        c.status is not CheckStatus.FAILED for c in d2.stops[0].checks
    )
    # 今天第 1 站只有 poi_exists + open_today 两条，没有跨天段那条
    assert len(d2.stops[0].checks) == 2


def test_validate_trip_cross_day_blocks_when_first_stop_is_far() -> None:
    """🔴 **M3 的核心验收场景**：故意构造一份"第 2 天第一站特别远"的行程。

    这正是一直漏掉的那个缺口：`Stop.from_prev_drive_min` 对当天第 1 站**恒为 0**
    （它没有"上一站"），于是远郊首站那一段从来没人判。

    这里塞一个"永远 200 分钟"的假距离函数 —— 不带真网络、完全确定。
    """
    d1 = day(1, [stop(JINLI)])
    d2 = day(2, [stop(QINGCHENG, seq=1)])
    for d in (d1, d2):
        d.day_stats = DayStats(distance_km=60, drive_min=170, walk_km=2)
    t = trip([d1, d2])

    report = run(validate_trip(t, _pool(), REQ, distance_fn=always(200)))
    assert not report.passed
    assert len(report.blocking) == 1, f"这一份只该有一条硬错（就是跨天段）：{report.blocking}"
    reason = report.blocking[0]
    assert "第 2 天" in reason, f"要指出是第 2 天：{reason}"
    assert _poi(JINLI).name in reason, f"要点名昨天的最后一站（锦里古街）：{reason}"
    assert _poi(QINGCHENG).name in reason, f"要点名今天第一站（青城山景区）：{reason}"
    assert "200" in reason, f"要写出实际车程：{reason}"


def test_validate_trip_cross_day_uses_previous_day_last_stop() -> None:
    """跨天段的**两端要对**：昨天**最后**一站 → 今天**第一**站。

    写错成"昨天第一站"或"今天最后一站"都会让判据失准，而且不容易发现 ——
    所以这里把两端都记下来断言。
    """
    seen: list[tuple] = []

    async def spy(origin, dest) -> DistanceResult:
        seen.append((origin, dest))
        return DistanceResult(km=10.0, drive_min=20, straight_km=8.0)

    d1 = day(1, [stop(WUHOU, seq=1), stop(JINLI, seq=2)])
    d2 = day(2, [stop(QINGCHENG, seq=1), stop(WUHOU, seq=2)])
    for d in (d1, d2):
        d.day_stats = DayStats(distance_km=60, drive_min=170, walk_km=2)
    t = trip([d1, d2])

    run(validate_trip(t, _pool(), REQ, distance_fn=spy))

    assert len(seen) == 1, f"跨天段只该打一次（第 2 天第 1 站），实际 {len(seen)} 次"
    origin, dest = seen[0]
    assert origin == (_poi(JINLI).lng, _poi(JINLI).lat), "起点该是昨天最后一站（锦里）"
    assert dest == (_poi(QINGCHENG).lng, _poi(QINGCHENG).lat), "终点该是今天第一站（青城山）"


def test_validate_trip_day_without_stats_is_unknown_not_pass() -> None:
    """`day_stats` 缺失（粘贴来的行程常见）→ 车程/步行两条都落 `unknown`。

    这里最容易偷懒成 `if stats: 判, else: pass` —— 那就等于
    "没有数据 = 通过"，正是三态要防的事。
    """
    d = day(1, [stop(WUHOU)])
    d.day_stats = None
    t = trip([d])

    report = run(validate_trip(t, _pool(), REQ))
    day_codes = {(c.code, c.status) for c in d.checks}
    assert ("reachable", CheckStatus.UNKNOWN) in day_codes
    assert ("walk_load", CheckStatus.UNKNOWN) in day_codes
    assert report.blocking == []


def test_validate_trip_uses_mock_provider_end_to_end() -> None:
    """把 `MockAmapProvider.calc_distance` 当真 `distance_fn` 接进来跑一遍。

    前面用的是"永远 200 分钟"的假函数（为了精确控制），
    这一条则验证**真实签名对接得上** —— 参数顺序、返回类型、`await`，
    任何一处对不上，这里会炸，而假函数那几条不会。
    """
    provider = MockAmapProvider(today=TODAY)
    d1 = day(1, [stop(JINLI)])
    d2 = day(2, [stop(QINGCHENG, seq=1)])
    for d in (d1, d2):
        d.day_stats = DayStats(distance_km=60, drive_min=80, walk_km=2)
    t = trip([d1, d2])

    report = run(validate_trip(t, _pool(), REQ, distance_fn=provider.calc_distance))
    assert report.passed, f"锦里→青城山（约 60km / 80 分钟）不该被判硬错：{report.blocking}"


def test_validate_trip_reports_hard_failed_and_unknown_separately() -> None:
    """`hard_failed` 与 `unknown` 是**两个独立列表**。

    合成一个列表 + 一个 status 字段会诱发"遍历时忘了过滤"，
    而这个错误的表现正好是"unknown 把行程打回了"。分开存是**结构上的防呆**。
    """
    d = day(1, [invented_stop()])
    d.day_stats = DayStats(distance_km=1, drive_min=5, walk_km=1)
    t = trip([d])

    report = run(validate_trip(t, _pool(), REQ))
    assert report.hard_failed and report.unknown
    assert report.hard_failed[0].status is CheckStatus.FAILED
    assert all(c.status is CheckStatus.UNKNOWN for c in report.unknown)
    assert len(report.blocking) == len(report.hard_failed)


def test_validate_trip_empty_trip_is_clean() -> None:
    """空行程不炸、也不产生任何 blocking（M1 骨架阶段会走到这里）。"""
    report = run(validate_trip(trip([day(1, [])]), _pool(), REQ))
    assert report.blocking == []
    assert report.passed
