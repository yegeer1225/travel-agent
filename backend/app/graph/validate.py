"""硬判据（5 类）+ 三态 —— **项目的核心**（`方案.md` 第六章）。

## 为什么判据要单独一个文件

1. `draft.py` 的职责是"**补事实**"（把模型出的骨架填成带坐标/距离的完整行程），
   判断是另一件事。混在一起，两边都改不动
2. **判据要能独立重跑** —— 界面二拖动站点后距离变了，判据要跟着重算，
   而重算不该把整份行程重新生成一遍
3. 纯函数好测：造一个 `Trip` → 跑判据 → 断言三态

## 五条判据

| code | 判什么 | 数据来源 | 会落 `unknown` 吗 |
|---|---|---|---|
| `poi_exists` | 这个地点是模型编的吗 | 候选池 | 不会（有池子就一定能判） |
| `open_today` | 到达时刻在营业时段内吗 | POI `open_time` | **会** |
| `reachable` | 车程在同行人承受范围内吗 | 距离接口（已被 `assemble_trip` 算进 `Stop`） | 不会 |
| `weather_conflict` | 恶劣天气撞上户外景点了吗 | 天气接口 + POI `type` | **会** |
| `walk_load` | 当天累计走路量超了吗 | 站数估算（**不是实测**） | 不会 |

## 三态不是"可选的礼貌"（`方案.md` 6.2）

`unknown` **只**出现在"数据拿不到"时：不通过、不打回、如实标注。
两态会逼出一个错误答案 —— 选"通过"是撒谎，选"不通过"是误打回（让 agent 白重排几轮）。
评测报告也要单列"无法判定"的占比，否则"通过率"会虚高。

## ⚠️ `open_today` 这个名字有个必须说清的局限

高德的 `open_time` **只有"每天的时间段"，没有"周几闭馆"这类信息**（2026-09-15 实测确认：
`biz_ext` 里只有 `open_time` / `opentime2`，后者是更啰嗦的同一天时段描述）。

所以它实际判的是「**到达时刻是否落在营业时段内**」，**不是**「那天是不是闭馆日」。
—— 名字沿用契约（`方案.md` 定的），但这个差别不能让人误会：

> 判据显示 ✅ 时，用户的读法是"那天它开门"。
> 而实际上代码只知道"你 09:30 去的时候它在营业时段内"。

📌 这也是**风险清单里"预约/闭馆日"只能靠 LLM 软判据**的原因 —— 高德压根没这个数据。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.graph.intent import Requirements, Travelers
from app.providers.base import DistanceResult
from app.schemas import AmapPoi, Check, CheckLevel, CheckStatus, Stop, Trip, Weather, WeatherStatus
from app.tools.poi_pool import PoiPool

DistanceFn = Callable[[tuple[float, float], tuple[float, float]], Awaitable[DistanceResult]]
"""算两点驾车距离的函数 —— 就是 `AmapProvider.calc_distance` 的签名。

**为什么做成参数而不是直接 import provider**：这样 `validate_trip` 保持纯粹，
测试可以塞一个"永远返回 200 分钟"的假函数来验跨天判据，不用起网络、不用 mock provider。
（参数是 `(lng, lat)` 元组，高德惯例。）"""

# ══════════════════════════════════════════════════════════════
#  阈值与关键词表
# ══════════════════════════════════════════════════════════════

MAX_HOP_DRIVE_MIN = 120
"""单程车程上限（分钟）。超过这个数，"这一跳"就不像是当日行程的一部分了。"""

MAX_HOP_DRIVE_MIN_WITH_ELDERS = 90
"""带老人时收紧到 90 分钟。

**为什么要按同行人分档**（`schemas.py` 里 `Travelers` 拆成大人/小孩/老人就是为了这个）：
带 1 位老人和带 1 位成年朋友，对"坐车 2 小时"的容忍度差一倍 ——
阈值是一个数的话，这条判据对两类用户必然有一类是错的。"""

MAX_DAY_DRIVE_MIN = 180
MAX_DAY_DRIVE_MIN_WITH_ELDERS = 150
"""当天累计车程上限。单程不超但一天跑了 5 段，同样是"赶路的一天"。"""

MAX_WALK_KM = 8.0
MAX_WALK_KM_WITH_ELDERS = 5.0
MAX_WALK_KM_WITH_CHILDREN = 6.0
"""当天累计步行量上限。

⚠️ 这个数字是**估算**不是实测：高德没有"两点间步行距离"这个数据，
   所以 `DayStats.walk_km` 是"站数 × 1.2 km"（见 `draft.py` 的 `WALK_KM_PER_STOP`）。
   **判据的强度因此有限** —— 它只能抓"排了 8 个站"这种量级问题，
   抓不到"这两个站在同一个公园里其实很近"。msg 里必须标明"估算"。"""

BAD_WEATHER_KEYWORDS = (
    "暴雨",
    "大暴雨",
    "特大暴雨",
    "暴雪",
    "大雪",
    "台风",
    "沙尘暴",
    "冰雹",
    "雷暴",
)
"""会真实打断户外行程的天气。

⚠️ **故意不含"阵雨""小雨""雷阵雨"**：实测查成都那几天预报是「阵雨」，
   如果算进来，一份正常行程会被天天打回 —— 而"下阵雨"对旅游实测**不构成冲突**。
   判据的假阳性比假阴性贵得多：假阴性只是少提醒一句，假阳性是让 agent 白烧几轮。"""

OUTDOOR_TYPE_KEYWORDS = ("风景名胜", "特色商业街", "公园广场", "动物园", "植物园")

_ALWAYS_OPEN_KEYWORDS = ("24小时", "全天", "24 小时")
_TIME_RANGE = re.compile(r"(\d{1,2}):(\d{2})\s*[-~—至]\s*(\d{1,2}):(\d{2})")


# ══════════════════════════════════════════════════════════════
#  小工具
# ══════════════════════════════════════════════════════════════


def _to_minutes(hhmm: str | None) -> int | None:
    if not hhmm:
        return None
    try:
        hh, mm = hhmm.split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


def parse_open_windows(text: str | None) -> list[tuple[int, int]] | None:
    """把高德的营业时间解析成 `[(起, 止)]`（分钟数）。

    | 原始值 | 解析结果 | 说明 |
    |---|---|---|
    | `'08:30-18:30'` | `[(510, 1110)]` | 常规 |
    | `'07:30-17:00 08:00-16:30'` | `[(450, 1020), (480, 990)]` | **旺季/淡季两套**，实测真有这种值 |
    | `'11:00-02:00'` | `[(660, 120)]` | **跨夜**（餐饮常见）→ 止 < 起，调用方按跨夜处理 |
    | `'24小时营业'` | `[(0, 1439)]` | |

    返回 `None` = 格式没见过 → 那条判据落 `unknown`，**不要去猜**。
    """
    if not text:
        return None
    if any(k in text for k in _ALWAYS_OPEN_KEYWORDS):
        return [(0, 1439)]

    windows: list[tuple[int, int]] = []
    for match in _TIME_RANGE.finditer(text):
        h1, m1, h2, m2 = (int(g) for g in match.groups())
        if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59:
            continue  # 形似但不是时间（比如 "1:99"）
        windows.append((h1 * 60 + m1, h2 * 60 + m2))
    return windows or None


def _in_window(minute: int, start: int, end: int) -> bool:
    if end >= start:
        return start <= minute <= end
    # 跨夜：11:00-02:00 → minute >= 660 或 minute <= 120
    return minute >= start or minute <= end


def is_outdoor(poi: AmapPoi | None) -> bool:
    """这个 POI 算不算"户外活动"。

    ⚠️ **判不了就当不是户外**（宁可漏判也不误判）。
       误判成户外会让一条好行程被暴雨判据打回；漏判只是少提醒一句。
       所以 `type` 缺失时返回 `False`，而不是"猜一下"。
    """
    if poi is None or not poi.type:
        return False
    return any(k in poi.type for k in OUTDOOR_TYPE_KEYWORDS)


def _with_elders(travelers: Travelers | None) -> bool:
    return bool(travelers and (travelers.elders or 0) > 0)


def _with_children(travelers: Travelers | None) -> bool:
    return bool(travelers and (travelers.children or 0) > 0)


def _hard(code: str, status: CheckStatus, msg: str | None = None) -> Check:
    return Check(level=CheckLevel.HARD, code=code, status=status, msg=msg)


# ══════════════════════════════════════════════════════════════
#  ① poi_exists —— 封闭世界的锚点
# ══════════════════════════════════════════════════════════════


def check_poi_exists(poi_id: str, pool: PoiPool) -> Check:
    """这个 POI 是不是工具真的返回过。

    **唯一一条"必要条件"判据** —— 它挂了，整条可信度链路就没了。
    也是唯一一条**不会**落 `unknown` 的：有候选池就一定判得了。

    ⚠️ 它回答的是「**是不是模型编的**」，**不**回答「**值不值得去**」（D36）。
       "武侯祠博物馆"和"武侯祠东侧门售票处(暂停营业)"在这里**打同一个 ✅**。
    """
    if pool.contains(poi_id):
        return _hard("poi_exists", CheckStatus.PASSED)
    return _hard(
        "poi_exists",
        CheckStatus.FAILED,
        f"POI id `{poi_id}` 不在候选池里 —— 这个地点不是工具搜出来的",
    )


# ══════════════════════════════════════════════════════════════
#  ② open_today —— 会落 unknown 的第一条
# ══════════════════════════════════════════════════════════════


def check_open_today(stop: Stop) -> Check:
    """到达时刻是否在营业时段内（**不是**"那天是不是闭馆日"，见模块 docstring）。"""
    if not stop.open_time:
        return _hard(
            "open_today",
            CheckStatus.UNKNOWN,
            "高德没有返回这个地点的营业时间，判不了 —— 不代表它不开门",
        )

    windows = parse_open_windows(stop.open_time)
    if windows is None:
        return _hard(
            "open_today",
            CheckStatus.UNKNOWN,
            f"营业时间格式没见过，判不了：{stop.open_time!r}",
        )

    arrive = _to_minutes(stop.arrive)
    if arrive is None:
        return _hard(
            "open_today",
            CheckStatus.UNKNOWN,
            "这一站没有安排到达时刻，判不了是否落在营业时段内",
        )

    if any(_in_window(arrive, start, end) for start, end in windows):
        return _hard("open_today", CheckStatus.PASSED)
    return _hard(
        "open_today",
        CheckStatus.FAILED,
        f"计划 {stop.arrive} 到达，而它的营业时间是 {stop.open_time}",
    )


# ══════════════════════════════════════════════════════════════
#  ③ reachable —— 单跳 + 当天累计
# ══════════════════════════════════════════════════════════════


def check_hop_drive(stop: Stop, prev_name: str | None, travelers: Travelers | None) -> Check:
    """从上一站到这一站的车程。（当天第 1 站没有"上一站"，判据在别处处理。）"""
    limit = MAX_HOP_DRIVE_MIN_WITH_ELDERS if _with_elders(travelers) else MAX_HOP_DRIVE_MIN
    if stop.from_prev_drive_min <= limit:
        return _hard("reachable", CheckStatus.PASSED)
    return _hard(
        "reachable",
        CheckStatus.FAILED,
        f"从「{prev_name or '上一站'}」到这里要开 {stop.from_prev_drive_min} 分钟，"
        f"超过 {limit} 分钟{'（带长辈，标准更严）' if _with_elders(travelers) else ''}",
    )


def check_day_drive(day_drive_min: int, travelers: Travelers | None) -> Check:
    """当天累计车程。单跳都不超但一天跑五段，同样是"赶路的一天"。"""
    limit = MAX_DAY_DRIVE_MIN_WITH_ELDERS if _with_elders(travelers) else MAX_DAY_DRIVE_MIN
    if day_drive_min <= limit:
        return _hard("reachable", CheckStatus.PASSED)
    return _hard(
        "reachable",
        CheckStatus.FAILED,
        f"这一天累计开车 {day_drive_min} 分钟，超过 {limit} 分钟"
        f"{'（带长辈）' if _with_elders(travelers) else ''}，太赶了",
    )


async def check_cross_day_hop(
    first: Stop,
    prev_last: Stop,
    distance_fn: DistanceFn,
    travelers: Travelers | None,
) -> Check:
    """**跨天段**：从「昨天最后一站」到「今天第一站」的车程。

    ## 这一条为什么必须单独存在

    2026-09-15 跑真实行程时发现的缺口：`Stop.from_prev_km` 对**当天第 1 站恒为 0**
    （它没有"上一站"），于是——

    > 一份 3 天行程里第 3 天排了都江堰（离成都市区 50+ km），
    > 而 `total_distance_km` 显示 1.3 km，`reachable` 判据也一声不吭。

    漏掉的恰恰是**最容易出问题的那一段**：远郊的第一站。

    ## 它**不是**"从住处出发"的距离

    用户没说住哪，需求抽取里也没有"住宿"这一项 —— 所以那个距离**算不出来**。
    这一条算的是「昨天最后一站 → 今天第一站」，它是前者的**下界**：

    不管昨晚回没回住处，这段路用户都得走，所以实际耗时 **≥** 这个值。
    拿**下界**做判据的好处是它**只会漏报，不会误报** ——
    而误报（把好行程判成坏的）会让 agent 白烧一轮，代价高得多。

    ## ⚠️ 所以它**绝不能**被加进 `total_distance_km`

    那是"看起来比实际小"的数字，而用户会拿总里程判断行程强度。
    `total_distance_km` 因此有个**已知的低估**（不含跨天段、不含往返住处）——
    记在 `DECISIONS.md` 第六章，不在 M3 偷偷改契约语义。
    """
    limit = MAX_HOP_DRIVE_MIN_WITH_ELDERS if _with_elders(travelers) else MAX_HOP_DRIVE_MIN
    try:
        result = await distance_fn((prev_last.lng, prev_last.lat), (first.lng, first.lat))
    except Exception as exc:  # noqa: BLE001
        return _hard(
            "reachable",
            CheckStatus.UNKNOWN,
            f"跨天段的距离没算出来（{type(exc).__name__}），判不了这段路要不要开很久",
        )

    if result.drive_min <= limit:
        return _hard("reachable", CheckStatus.PASSED)
    return _hard(
        "reachable",
        CheckStatus.FAILED,
        f"从昨天的最后一站「{prev_last.name}」到今天第一站「{first.name}」要开 "
        f"{result.drive_min} 分钟（{result.km} km），超过 {limit} 分钟"
        f"{'（带长辈）' if _with_elders(travelers) else ''} —— 换到别的天，或换个更近的起点",
    )


# ══════════════════════════════════════════════════════════════
#  ④ weather_conflict —— 会落 unknown 的第二条
# ══════════════════════════════════════════════════════════════


def check_weather_conflict(weather: Weather | None, stops: list[Stop], pool: PoiPool) -> Check:
    """恶劣天气撞上户外景点。

    ⚠️ 这一条的**假阳性代价特别高**：判错了会让一份本来挺合理的行程被打回重排，
       而"重排"意味着又一次生成（钱 + 时间）。
       所以关键词表故意只收"暴雨/台风"这一档，不含"阵雨"（见常量注释）。

    ⚠️ 判"户外"依赖 POI 的 `type`，而手写/粘贴来的行程可能没这个字段
       → 那就当"不是户外"（不漏判成冲突），判据落 `unknown` 更诚实吗？
       不 —— 这里选择 PASSED 而不是 UNKNOWN：
       因为"天气好"这个条件本身是成立的，只是"有没有户外景点"判不了。
       落 UNKNOWN 会让一条本该绿的判据变灰，反而更让人困惑。
       **代价**：确实可能漏掉"雨天排了户外景点"这一种情况（记为已知局限）。
    """
    if weather is None or weather.status is not WeatherStatus.OK:
        return _hard(
            "weather_conflict",
            CheckStatus.UNKNOWN,
            "这一天的天气没查到，判不了会不会冲突",
        )

    text = f"{weather.day_weather or ''}{weather.night_weather or ''}"
    bad = next((k for k in BAD_WEATHER_KEYWORDS if k in text), None)
    if bad is None:
        return _hard("weather_conflict", CheckStatus.PASSED)

    outdoor = [s for s in stops if is_outdoor(pool.get(s.poi_id))]
    if not outdoor:
        return _hard("weather_conflict", CheckStatus.PASSED)

    names = "、".join(s.name for s in outdoor[:3])
    more = f" 等 {len(outdoor)} 处" if len(outdoor) > 3 else ""
    return _hard(
        "weather_conflict",
        CheckStatus.FAILED,
        f"预报有「{bad}」，而当天安排的是户外景点（{names}{more}）",
    )


# ══════════════════════════════════════════════════════════════
#  ⑤ walk_load —— 累计走路量 vs 同行人
# ══════════════════════════════════════════════════════════════


def _walk_limit(travelers: Travelers | None) -> tuple[float, str]:
    if _with_elders(travelers):
        return MAX_WALK_KM_WITH_ELDERS, "同行有长辈，"
    if _with_children(travelers):
        return MAX_WALK_KM_WITH_CHILDREN, "同行有小孩，"
    return MAX_WALK_KM, ""


def check_walk_load(walk_km: float, travelers: Travelers | None) -> Check:
    """当天累计步行量。

    ⚠️ `walk_km` 是**估算**（站数 × 1.2km），不是实测 —— 高德没有步行距离数据。
       msg 里必须写"估算"，否则用户会以为这是算出来的精确值。
       这条判据能抓"一天排 8 个站"，抓不到"这两站其实在同一个公园里"。
    """
    limit, reason = _walk_limit(travelers)
    if walk_km <= limit:
        return _hard("walk_load", CheckStatus.PASSED)
    return _hard(
        "walk_load",
        CheckStatus.FAILED,
        f"当天要走约 {walk_km} km（估算值），{reason}超过 {limit} km 的上限",
    )


# ══════════════════════════════════════════════════════════════
#  汇总
# ══════════════════════════════════════════════════════════════


@dataclass
class ValidationReport:
    """一次校验的结论。"""

    blocking: list[str] = field(default_factory=list)
    """**硬错的人话**，直接喂给 `repair` 作为打回理由。只有 `FAILED` 的硬判据会进来 ——
    `UNKNOWN` 绝不进来（那正是三态存在的意义）。"""

    hard_failed: list[Check] = field(default_factory=list)
    unknown: list[Check] = field(default_factory=list)
    """判不了的那些。**不打回，也不算通过** —— 交给前端显示灰色。"""

    @property
    def passed(self) -> bool:
        return not self.blocking


async def validate_trip(
    trip: Trip,
    pool: PoiPool,
    requirements: Requirements,
    *,
    distance_fn: DistanceFn | None = None,
) -> ValidationReport:
    """跑全部 5 条硬判据，**原地把结果写进 `trip.days[].checks` / `trip.days[].stops[].checks`**。

    ⚠️ **每次调用先清空 checks**（幂等）。
       不清的话，同一份行程校验两次 → 每个判据出现两遍 → 前端满屏重复红标，
       而且这个 bug 只在"重算"路径上出现（首次生成看不出来，很难在现场发现）。

    `distance_fn` 传了才算跨天段（`check_cross_day_hop`）；不传就跳过那一条 ——
    跳过是**有意的默认值**，因为"跨天段"要额外打一次高德接口，
    而重算场景（拖拽后只改了顺序）不该被它拖慢。
    """
    travelers = requirements.travelers
    report = ValidationReport()
    prev_day_last: Stop | None = None

    for day in trip.days:
        day.checks = []

        # ── 站点级 ──
        prev_name: str | None = None
        for index, stop in enumerate(day.stops):
            checks = [check_poi_exists(stop.poi_id, pool), check_open_today(stop)]
            if prev_name is not None:
                checks.append(check_hop_drive(stop, prev_name, travelers))
            elif index == 0 and prev_day_last is not None and distance_fn is not None:
                # 当天第 1 站：没有"当天上一站"，但**上一站的跨天段是真的要走**（见函数注释）
                checks.append(
                    await check_cross_day_hop(stop, prev_day_last, distance_fn, travelers)
                )
            stop.checks = checks
            prev_name = stop.name

        if day.stops:
            prev_day_last = day.stops[-1]

        # ── 天级 ──
        day.checks.append(check_weather_conflict(day.weather, day.stops, pool))

        stats = day.day_stats
        if stats is None:
            day.checks.append(
                _hard("reachable", CheckStatus.UNKNOWN, "这一天没有距离统计，判不了车程")
            )
            day.checks.append(
                _hard("walk_load", CheckStatus.UNKNOWN, "这一天没有步行统计，判不了走路量")
            )
        else:
            day.checks.append(check_day_drive(stats.drive_min, travelers))
            day.checks.append(check_walk_load(stats.walk_km, travelers))

        # ── 汇总 ──
        for check in [*day.checks, *(c for s in day.stops for c in s.checks)]:
            if check.status is CheckStatus.UNKNOWN:
                report.unknown.append(check)
            elif check.status is CheckStatus.FAILED and check.level is CheckLevel.HARD:
                report.hard_failed.append(check)
                report.blocking.append(_describe(day, check))

    return report


def _describe(day, check: Check) -> str:
    """把一条失败判据说成一句能直接喂给模型的话。

    ⚠️ **带上"第几天"**：模型拿到"车程 130 分钟超限"会不知道改哪天，
       而重排时它手里是整份行程。
    """
    return f"第 {day.day} 天（{day.date.isoformat() if day.date else '无日期'}）{check.code}：{check.msg}"


__all__ = [
    "BAD_WEATHER_KEYWORDS",
    "DistanceFn",
    "MAX_DAY_DRIVE_MIN",
    "MAX_HOP_DRIVE_MIN",
    "MAX_WALK_KM",
    "OUTDOOR_TYPE_KEYWORDS",
    "ValidationReport",
    "check_cross_day_hop",
    "check_day_drive",
    "check_hop_drive",
    "check_open_today",
    "check_poi_exists",
    "check_walk_load",
    "check_weather_conflict",
    "is_outdoor",
    "parse_open_windows",
    "validate_trip",
]
