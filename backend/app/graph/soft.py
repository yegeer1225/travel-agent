"""软判据 4 类 —— **只产出「提醒」，不产出「事实断言」**（D45）。

═══════════════════════════════════════════════════════════════
 这个模块存在的唯一理由：硬判据有守门人，软判据没有
═══════════════════════════════════════════════════════════════

硬判据靠**封闭世界校验**守门 —— 模型写的地点必须能在候选池里找到，
所以它编不出来。软判据由 LLM 判，**没有对应的守门人**，于是它最自然的输出是：

    ❌ 「武侯祠需要提前 3 天预约」

系统**根本没有预约政策这个数据源**，这句话是模型凭记忆编的。
而它一旦出现在界面上，这个项目"结果不是模型编的"这条立身之本就破了 ——
面试官只要追问一句"这个 3 天你哪来的"就崩。

⚠️ 所以要区分两种输出（D45）：

| | 例子 | 为什么可接受 |
|---|---|---|
| ❌ **事实断言** | "需提前 3 天预约"、"排队约 2 小时"、"周一闭馆" | 无数据源，且**政策类事实会变**，无法查证 |
| ✅ **提醒** | "武侯祠是热门景点，节假日建议提前确认预约政策" | 不含任何无来源的信息，且**对用户真的有用** |
| ✅ **已知事实的解读** | "第 2 天步行 6.8km，带两位长辈偏紧" | 6.8km **是我们自己算的**，模型只是解读它 |

第三条是最值钱的一类：它的每个数字都来自输入，**编不出来**。

═══════════════════════════════════════════════════════════════
 越界检查是「近似」的，这一点必须写在代码里
═══════════════════════════════════════════════════════════════

`find_overreach` 用的是**模式黑名单**，不是完备的语法分析。它抓不住的越界确实存在
（比如模型用完全不含关键词的句子把同一个意思说出来）。

那为什么不干脆不做检查？因为：

1. 抓不住全部 ≠ 抓不住典型。真正高频的越界就那几种形态，模式覆盖得不错
2. 命中的那条**会被丢弃**（不写进 `Trip`）—— 宁可少一条提醒，不可留一句编的话
3. 丢弃是**可观测**的（返回在 `SoftApplyResult.dropped` 里），
   M8 评测可以把"丢弃率"当成一个指标盯着

⚠️ **不要把它包装成"完备的防幻觉"** —— 它是"给最典型的越界形态上了一道闸"。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.graph.intent import Requirements
from app.schemas import Check, CheckLevel, CheckStatus, Trip

logger = logging.getLogger(__name__)
"""软判据是 best-effort：失败不抛、不进 `state["error"]` —— **日志是它唯一的可观测面**。
没有日志的话，2026-09-18 实测那种"soft_check 0.2 秒结束、0 条软提醒"的静默失败永远查不到根因。"""

# ══════════════════════════════════════════════════════════════
#  判据清单
# ══════════════════════════════════════════════════════════════


class SoftCheckCode(StrEnum):
    """4 条软判据（`方案.md` 6.1）。**`fail` 只提醒，永远不打回重排。**"""

    NEEDS_BOOKING = "needs_booking"
    QUEUE_TIME = "queue_time"
    ELDER_FRIENDLY = "elder_friendly"
    OVERALL_FEASIBLE = "overall_feasible"


#: 每条判据落在哪一层。**这张表是"判据主语"的唯一真源** ——
#: `apply_soft_findings` 按它决定挂 `Stop` / `Day` / `Trip`，模型说了不算。
#: （模型给 `day`/`seq` 只是"它认为指哪"，合法性由这里兜底。）
LEVEL_OF: dict[SoftCheckCode, str] = {
    SoftCheckCode.NEEDS_BOOKING: "stop",
    SoftCheckCode.QUEUE_TIME: "stop",
    SoftCheckCode.ELDER_FRIENDLY: "day",
    SoftCheckCode.OVERALL_FEASIBLE: "trip",
}

_WHAT_IT_MEANS = {
    SoftCheckCode.NEEDS_BOOKING: (
        "这个地点是热门/限流的吗？值不值得提醒用户**去确认预约政策**。\n"
        "    ⚠️ 你不能回答「需要预约」—— 我们没有这个数据。你只能说「建议确认」。"
    ),
    SoftCheckCode.QUEUE_TIME: (
        "这一天的人流压力。依据只有**日期落在周末/长假内**这个事实。\n"
        "    ⚠️ 你不能给「排队多久」的估计，只能提醒「可能人流上升，建议早点到」。"
    ),
    SoftCheckCode.ELDER_FRIENDLY: (
        "**这一天**的安排对同行人吃不吃力。这是你最该发挥的一条 ——\n"
        "    因为步行量、车程、站数**都是我们算好给你的**，你只要解读它们。\n"
        "    ✅ 可以说「第 2 天步行 6.8km + 车程 140 分钟，带两位长辈偏紧」（数字全来自输入）\n"
        "    ❌ 不能说「青城山不适合老人」（这是一个我们没法查证的断言）"
    ),
    SoftCheckCode.OVERALL_FEASIBLE: (
        "整份行程的**整体节奏**。同样只依据已知事实（总天数、总站数、每天的站数）。"
    ),
}


# ══════════════════════════════════════════════════════════════
#  Prompt
# ══════════════════════════════════════════════════════════════

SOFT_SYSTEM_PROMPT_TEMPLATE = """你在给一份**已经排好的行程**做软性提醒。

你不负责判断行程对不对（那是代码做的），你负责**提醒用户那些值得提前知道的风险**。

═══════════════════════════════════════════════════════════════
 🔴 三条铁律 —— 违反任意一条，你这条提醒会被**整条丢弃**
═══════════════════════════════════════════════════════════════

**① 不许出现任何无来源的具体数字。**

你没有预约政策、票价、排队时长、限流人数、闭馆日的任何数据源。
写出来就是编造，整条会被丢掉。

    ❌ "需提前 3 天预约"  ❌ "排队约 2 小时"  ❌ "每天限流 5000 人"
    ❌ "门票 60 元"       ❌ "周一闭馆"

    ✅ 但**我给过你的数字可以引用** —— 步行量、车程、站数、天数、温度。
       它们本来就是这份提醒最有价值的部分。

**② 政策类的事只能「建议确认」，不能「断言」。**

    ❌ "武侯祠需要预约"          ✅ "武侯祠节假日建议提前确认预约政策"
    ❌ "青城山周一不开放"        ✅ "出发前建议确认一下当天是否正常开放"
    ❌ "这家店要排队一小时"      ✅ "周六中午的热门店通常人流较大，建议错峰"

**③ 只能用我给你的清单里的地点名。**

不许引入清单外的任何地点、活动、政策名。你提到山、博物馆、寺庙这类**类别词**可以，
但**具体名字**必须来自清单。

═══════════════════════════════════════════════════════════════
 四条判据分别判什么
═══════════════════════════════════════════════════════════════

- `needs_booking`（**站级**）：{needs_booking}

- `queue_time`（**站级**）：{queue_time}

- `elder_friendly`（**天级**）：{elder_friendly}

- `overall_feasible`（**行程级**）：{overall_feasible}

═══════════════════════════════════════════════════════════════
 三态：拿不准就写 unknown，**不要猜**
═══════════════════════════════════════════════════════════════

- `pass`   —— 确认没风险（**这时 `msg` 可以省略**，不要为了凑数写废话）
- `fail`   —— 确认有风险，需要提醒用户。`msg` 必填
- `unknown`—— 信息不足，判不了。`msg` 必填，**要说清是缺了什么**

⚠️ **`unknown` 不是失败，是诚实。** 缺日期就判不了旺季 → 写 `unknown`。
把 `unknown` 写成 `pass` 是撒谎，而撒谎在这个项目里是最严重的问题。

═══════════════════════════════════════════════════════════════
 输出
═══════════════════════════════════════════════════════════════

只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释文字：

{{"findings": [
  {{"code": "elder_friendly", "day": 2, "seq": null, "status": "fail",
    "msg": "第 2 天步行 6.8 公里、车程 140 分钟，带两位长辈可能偏紧，可以考虑减少一站"}},
  {{"code": "needs_booking", "day": 1, "seq": 1, "status": "fail",
    "msg": "武侯祠是热门景点，又赶上周五，建议出发前确认一下预约政策"}},
  {{"code": "overall_feasible", "day": null, "seq": null, "status": "pass"}}
]}}

`day` / `seq` 的填法：站级判据两个都填；天级判据只填 `day`；行程级两个都不填（null）。
**`status=pass` 的条目可以有 `msg` 也可以省略。**
"""

# ⚠️ 用 `.format()` 填三处判据说明 —— 四个 `{code}` 占位符和 JSON 例子里那些
#    转义的花括号（`{{`/`}}`）必须配套，改文案时别只改一边。
SOFT_SYSTEM_PROMPT = SOFT_SYSTEM_PROMPT_TEMPLATE.format(
    **_WHAT_IT_MEANS,  # StrEnum 的键正好是 code 字符串，可以直接展开
)


def build_soft_prompt(trip: Trip, req: Requirements) -> str:
    """把行程压成"只含事实"的清单喂给模型。

    🔴 **只给事实，不给判断** —— 这里每写一个形容词，都是在引导模型复述它而不是解读它。
    所以站名后面跟的是 `[类型]` 和数字，不是"值得去""很热门"。
    """
    tv = req.travelers
    who = "未说明"
    if tv is not None:
        parts = []
        if tv.adults:
            parts.append(f"{tv.adults} 位成人")
        if tv.elders:
            parts.append(f"{tv.elders} 位长辈")
        if tv.children:
            parts.append(f"{tv.children} 个小孩")
        who = "、".join(parts) or "未说明"

    lines = [
        "【整份行程的事实】",
        f"目的地：{trip.destination}",
        f"共 {len(trip.days)} 天",
        f"同行人：{who}",
        f"总站数：{sum(len(d.stops) for d in trip.days)}",
    ]
    if req.preferences:
        lines.append(f"用户偏好：{'、'.join(req.preferences)}")
    if req.budget is not None:
        lines.append(f"人均预算：{req.budget:g} 元（这是用户自己说的）")

    for d in trip.days:
        head = f"\n【第 {d.day} 天】"
        if d.date:
            wd = "周" + "一二三四五六日"[d.date.weekday()]
            head += f"{d.date.isoformat()}（{wd}）"
        else:
            head += "日期未定"
        if d.weather is not None and d.weather.status.value == "ok":
            temp = "~".join(
                str(v) for v in (d.weather.night_temp, d.weather.day_temp) if v is not None
            )
            head += f"　天气：{d.weather.day_weather or '?'}"
            if temp:
                head += f" {temp}℃"
        elif d.weather is not None:
            head += "　天气：查不到"
        lines.append(head)

        for s in d.stops:
            bits = [f"  {s.seq}. {s.name}"]
            if s.arrive:
                bits.append(f"{s.arrive} 到")
            bits.append(f"待 {s.stay_min} 分钟")
            if s.from_prev_km:
                bits.append(f"车程 {s.from_prev_drive_min} 分钟 / {s.from_prev_km:g} 公里")
            lines.append("  ".join(bits))
            # ⚠️ `Stop` **没有** `type` 字段（那在 `AmapPoi` 上）。
            # 用 `match_reason` 代替：它是"这一站为什么适合这个人"，
            # 对软判据（要不要预约 / 对长辈吃不吃力）来说比类别更有用。
            if s.match_reason:
                lines.append(f"      （安排理由：{s.match_reason[:60]}）")

        if d.day_stats is not None:
            lines.append(
                f"  ── 当天合计：车程 {d.day_stats.drive_min} 分钟，"
                f"站间 {d.day_stats.distance_km:g} 公里，步行 {d.day_stats.walk_km:g} 公里"
            )

    if not trip.days or not any(d.stops for d in trip.days):
        lines.append("\n（这份行程里一个站都没有 —— 这种情况基本什么都判不了。）")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
#  模型输出
# ══════════════════════════════════════════════════════════════


class SoftFinding(BaseModel):
    """模型给的一条软判据。

    ⚠️ **`extra="ignore"` 而不是 `forbid"`**：这是**模型输出**不是我们的契约。
    它偶尔多带一个字段（比如自作主张加个 `reason`），
    用 `forbid` 会让整条解析失败 —— 一个多余的键废掉四条判据，不划算。
    （同 `intent.py` 的取舍：契约用 `forbid` 保护自己，模型输出用 `ignore` 宽容。）
    """

    model_config = ConfigDict(extra="ignore")

    code: SoftCheckCode
    day: int | None = None
    seq: int | None = None
    status: CheckStatus
    msg: str | None = None


def parse_soft_findings(text: str) -> tuple[list[SoftFinding], str | None]:
    """解析模型输出。返回 `(findings, 错误说明)`。

    宽容处：根对象可能是 `{"findings": [...]}`，也可能模型直接给一个数组。
    **单条解析失败只丢那一条** —— 四条判据不该被一条脏数据全部废掉。
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], f"不是合法 JSON：{exc}"

    if isinstance(raw, dict):
        items = raw.get("findings")
    else:
        items = raw
    if not isinstance(items, list):
        return [], "顶层没有 findings 数组"

    # 🔴 **空数组是合法答案，不是解析失败。**
    # 「这次没什么要提醒的」正是 `pass` 之外最常见的结果 ——
    # 把它当成失败会让每次正常跑都白白重试一次 LLM，并在 `soft_report.error` 里
    # 留下一个假的失败标记（而那个字段是"软判据有没有正常工作"的观测点）。
    if not items:
        return [], None

    out: list[SoftFinding] = []
    bad = 0
    for item in items:
        try:
            out.append(SoftFinding.model_validate(item))
        except ValidationError:
            bad += 1
    if not out:
        return [], f"{len(items)} 条里没有一条能解析"
    return out, (f"{bad} 条解析失败已跳过" if bad else None)


# ══════════════════════════════════════════════════════════════
#  越界检查（D45 的"三条铁律"变成可执行的检查）
# ══════════════════════════════════════════════════════════════

#: 每个模式都绑定一个**我们确实没有的信息类别** —— 所以误伤率低。
#: 这是关键：不能笼统地"禁止数字"（"这 3 天"是合法的、输入里就有），
#: 要禁的是"**特定类别的**无来源数字"。
_OVERREACH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("金额", re.compile(r"[¥￥]\s*\d|\d+\s*(?:元|块钱)|门票\s*\d")),
    ("预约提前量", re.compile(r"提前\s*[0-9一二三四五六七八九十两]+\s*[天日周]")),
    ("限额", re.compile(r"(?:限流|限额|限量|最多|每天限|每日限)\s*[0-9一二三四五六七八九十两]+")),
    ("排队时长", re.compile(r"排队[^。；;]{0,8}[0-9一二三四五六七八九十两]+\s*(?:小时|分钟)")),
    ("闭馆日", re.compile(r"周[一二三四五六日天]\s*(?:闭馆|闭园|休馆|不开放|不开)")),
)

#: 「不预约就进不去」这类**反面断言** —— 和"需要预约"是同一件事的两种说法。
#: 单独一个模式是因为它**不含"需要/必须"**，上面那条匹配不到。
_NO_BOOKING_NO_ENTRY = re.compile(
    r"不\s*(?:提前)?\s*预约[^。；;]{0,8}(?:进不去|进不了|不能进|入不了|无法进|白跑)"
)

#: 「需要预约」这类**政策断言**。⚠️ 只有当整句里**没有"确认"**时才算越界 ——
#: 因为合规的写法就是"建议**确认**预约政策"，它本身就含"预约"两个字。
_BOOKING_CLAIM = re.compile(r"(?:需要|必须|一定要|得)\s*预约|预约才能|不预约[^。；;]{0,4}不[^。；;]{0,4}(?:进|入)")


def find_overreach(msg: str | None) -> list[str]:
    """返回这条 `msg` 违反了哪几条铁律（空列表 = 合规）。

    ⚠️ **近似检查，不是完备检查**（模块 docstring 已说明）。
    它挡不住"用别的说法说出同一件事"，只挡最典型的形态。
    """
    if not msg:
        return []
    hits = [label for label, pat in _OVERREACH_PATTERNS if pat.search(msg)]
    # ⚠️ 只有整句里**没有"确认"**时才把"需要预约"算越界 ——
    #    因为唯一合规的写法就是"建议**确认**预约政策"，它本身就含"预约"两个字。
    if "确认" not in msg and (_BOOKING_CLAIM.search(msg) or _NO_BOOKING_NO_ENTRY.search(msg)):
        hits.append("预约断言")
    return hits


# ══════════════════════════════════════════════════════════════
#  写入 Trip
# ══════════════════════════════════════════════════════════════


@dataclass
class SoftApplyResult:
    """一次软校验的账。**丢弃的条目必须能追溯到原因** —— 否则"丢了几条"没人看得见。"""

    applied: int = 0
    warnings: int = 0
    unknown: int = 0
    dropped: list[str] = field(default_factory=list)
    """被丢弃的条目，形如 `"needs_booking day1 seq1：违反了[预约提前量]"`。
    M8 评测的"丢弃率"就数这个列表。"""

    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _target_of(trip: Trip, finding: SoftFinding) -> Any:
    """按 `LEVEL_OF` 找到这条判据该挂的对象。找不到返回 `None`。

    🔴 **层级由 `LEVEL_OF` 决定，不由模型说了算。**
    模型给错 `day`/`seq` 的后果是"这条挂不上去"（丢弃），
    而不是"挂到一个错的地方" —— 后者会污染一份本来干净的行程。
    """
    level = LEVEL_OF[finding.code]
    if level == "trip":
        return trip

    if finding.day is None:
        return None
    day = next((d for d in trip.days if d.day == finding.day), None)
    if day is None:
        return None
    if level == "day":
        return day

    if finding.seq is None:
        return None
    return next((s for s in day.stops if s.seq == finding.seq), None)


def apply_soft_findings(trip: Trip, findings: list[SoftFinding]) -> SoftApplyResult:
    """把模型给的软判据**原地**写进 `trip` 的三层 `checks`。

    ⚠️ **先清空再写**，同 `validate.validate_trip`：不清的话"重新深度检查"
    跑两次 → 每条判据出现两遍 → 前端满屏重复灰标。
    """
    trip.checks = []
    for d in trip.days:
        d.checks = [c for c in d.checks if c.level is not CheckLevel.SOFT]
        for s in d.stops:
            s.checks = [c for c in s.checks if c.level is not CheckLevel.SOFT]

    result = SoftApplyResult()

    for finding in findings:
        target = _target_of(trip, finding)
        if target is None:
            result.dropped.append(
                f"{finding.code} day={finding.day} seq={finding.seq}："
                f"行程里没有对应的位置"
            )
            continue

        # 铁律②的一条硬性后果：**fail / unknown 必须带 msg**。
        # 没有 msg 的 fail 等于"告诉你这里有问题但不说什么问题"，用户没法行动。
        if finding.status is not CheckStatus.PASSED and not (finding.msg or "").strip():
            result.dropped.append(f"{finding.code}：{finding.status} 但没给 msg")
            continue

        overreach = find_overreach(finding.msg)
        if overreach:
            # 🔴 **整条丢弃，不是改写。** 改写要我们自己造一句话，
            #    那就等于我们用代码编了一条提醒 —— 同样是编，只是编的人换了。
            result.dropped.append(
                f"{finding.code}：违反铁律「{'、'.join(overreach)}」→ {(finding.msg or '')[:40]}"
            )
            continue

        target.checks.append(
            Check(
                code=finding.code.value,
                level=CheckLevel.SOFT,
                status=finding.status,
                msg=(finding.msg or "").strip() or None,
            )
        )
        result.applied += 1
        if finding.status is CheckStatus.FAILED:
            result.warnings += 1
        elif finding.status is CheckStatus.UNKNOWN:
            result.unknown += 1

    trip.summary.soft_warnings = result.warnings
    return result


# ══════════════════════════════════════════════════════════════
#  跑一次
# ══════════════════════════════════════════════════════════════


async def run_soft_checks(
    trip: Trip,
    req: Requirements,
    llm: BaseChatModel,
    *,
    max_attempts: int = 2,
) -> SoftApplyResult:
    """调一次 LLM，产出 4 条软判据并写进 `trip`。

    🔴 **一次调用判完全部，不是每站一次。**
    8 个站 × 4 条判据逐个调用 = 32 次 LLM，而每次要 3~8 秒 ——
    那是几分钟的等待换几句话，不划算。这里是**一份行程一次调用**。

    ⚠️ **失败不抛异常**（同 `nodes.py` 的模块纪律 ②）：
    软判据本来是"锦上添花"，为了它让整份行程拿不到，代价完全不对等。
    失败时返回带 `error` 的 result，`Trip` 里一条软判据都不写 —— 清空是对的行为，
    因为上一轮的结论已经跟着行程一起作废了。
    """
    base = build_soft_prompt(trip, req)
    last_err: str | None = None

    for _ in range(max(1, max_attempts)):
        messages: list[Any] = [SystemMessage(content=SOFT_SYSTEM_PROMPT)]
        messages.append(HumanMessage(content=base))
        if last_err:
            messages.append(
                HumanMessage(
                    content=(
                        f"你上一次的输出不合格：{last_err}\n"
                        f"请修正后**重新输出完整 JSON**（不要片段，不要解释）。"
                    )
                )
            )
        try:
            raw = await llm.bind(response_format={"type": "json_object"}).ainvoke(messages)
        except Exception as exc:  # noqa: BLE001
            # 🔴 失败必须带堆栈进日志：这里不抛、不写 state["error"]，
            #    soft_report.error 只有 CLI/评测能看到 —— 用户侧表现是
            #    "行程正常但一条软提醒都没有"，日志是唯一能钉死根因的地方。
            logger.error("软判据 LLM 调用失败（best-effort，行程照常输出）", exc_info=True)
            result = apply_soft_findings(trip, [])
            result.error = f"模型调用失败：{type(exc).__name__}: {exc}"
            return result

        text = getattr(raw, "content", "") or ""
        if not isinstance(text, str):
            text = str(text)
        findings, last_err = parse_soft_findings(text)
        # 「这次成功了」= 解析没出错，**或者**至少解析出了几条（部分脏数据不算失败）。
        # ⚠️ **不能写成 `if findings:`** —— 「一条都没提醒」是合法答案，
        #    用后者会让每次正常跑都白重试一次 LLM。
        # ⚠️ 也**不能写成 `if last_err is None:`** —— 那会把"4 条里坏了 1 条"
        #    误判成彻底失败，把好的 3 条扔掉。
        if last_err is None or findings:
            result = apply_soft_findings(trip, findings)
            if last_err:
                result.dropped.append(f"（解析提示）{last_err}")
            return result

    result = apply_soft_findings(trip, [])
    result.error = f"模型连续 {max_attempts} 次输出都不合格：{last_err}"
    logger.error("软判据连续不合格（best-effort，行程照常输出）：%s", result.error)
    return result


__all__ = [
    "LEVEL_OF",
    "SOFT_SYSTEM_PROMPT",
    "SoftApplyResult",
    "SoftCheckCode",
    "SoftFinding",
    "apply_soft_findings",
    "build_soft_prompt",
    "find_overreach",
    "parse_soft_findings",
    "run_soft_checks",
]
