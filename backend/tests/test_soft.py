"""软判据自检 —— **守住"D45 三条铁律"这一条纪律**。

═══════════════════════════════════════════════════════════════
 这批测试真正要守的三件事
═══════════════════════════════════════════════════════════════

1. **编造的话一句都不许进 `Trip`。**
   硬判据有 `poi_exists` 守着，软判据**没有守门人**（它由 LLM 判）——
   所以它唯一的保障就是"命中越界模式就整条丢弃"。
   每条模式（提前量 / 排队时长 / 限额 / 金额 / 闭馆日 / 预约断言）
   都要有测试**证明它真的会被拦下**，否则那条闸门等于不存在。

2. **不误伤。**
   丢弃是有代价的：少一条提醒。如果检查器把"第 2 天步行 6.8 公里"
   这种**数字全部来自输入**的合规提醒也拦掉，那软判据就什么都不剩了。
   → 所以每条模式都要配一个"**看着像但不是**"的反例。

3. **层级由代码定，不由模型说了算。**
   模型给错 `day`/`seq` 的后果必须是"这条挂不上去"，
   **不是**"挂到另一个地方"——后者会污染一份本来干净的行程，
   而且症状是"某天莫名多了一条别的地方的提醒"，极难查。

⚠️ **这里不测"模型判得对不对"** —— 那没有 ground truth（D45）。
   `fail`/`pass` 是不是判得准，属于 M8 评测的"可验证类阈值断言"和
   "不可验证类措辞合规率"，不是单元测试能回答的问题。
   单元测试只回答：**闸门通不通、层级挂得对不对、失败会不会拖垮流程**。
"""

from __future__ import annotations

from datetime import date, datetime

from app.graph.soft import (
    LEVEL_OF,
    SOFT_SYSTEM_PROMPT,
    SoftCheckCode,
    SoftFinding,
    apply_soft_findings,
    build_soft_prompt,
    find_overreach,
    parse_soft_findings,
    run_soft_checks,
)
from app.graph.intent import Requirements, Travelers
from app.schemas import (
    CheckLevel,
    CheckStatus,
    Day,
    DayStats,
    Stop,
    Trip,
    TripSource,
    TripSummary,
)
from fakes import ScriptedChatModel, run, soft_says

TODAY = date(2026, 9, 17)

# mock 池里的真实 POI（坐标取真值，软判据也读 `Stop` 上的数字）
WUHOU = "B001C07VJ2"
JINLI = "B0FFFD3P2C"
QINGCHENG = "B001C06ESL"


# ══════════════════════════════════════════════════════════════
#  造数据
# ══════════════════════════════════════════════════════════════


def _stop(seq: int, name: str, poi_id: str, **kw) -> Stop:
    base = dict(
        seq=seq,
        name=name,
        poi_id=poi_id,
        lng=104.05 + seq / 100,
        lat=30.65 + seq / 100,
        arrive=f"{8 + seq:02d}:00",
        stay_min=90,
        from_prev_km=float(seq),
        from_prev_drive_min=seq * 10,
    )
    base.update(kw)
    return Stop(**base)


def _trip(*, days: int = 1, elders: int | None = None) -> Trip:
    """一份两天、每天两站的行程。**数字不重要，结构重要。**"""
    d: list[Day] = []
    for n in range(1, days + 1):
        stops = [
            _stop(1, "武侯祠博物馆", WUHOU),
            _stop(2, "锦里古街", JINLI),
        ]
        d.append(
            Day(
                day=n,
                date=TODAY,
                theme=f"第{n}天",
                stops=stops,
                day_stats=DayStats(distance_km=4.4, drive_min=24, walk_km=4.1),
            )
        )
    return Trip(
        trip_id="t1",
        title="测试行程",
        destination="成都",
        source=TripSource.GENERATED,
        created_at=datetime(2026, 9, 16, 10, 0),
        updated_at=datetime(2026, 9, 16, 10, 0),
        days=d,
        summary=TripSummary(
            total_distance_km=8.8, stop_count=2 * days, hard_errors=0, soft_warnings=0
        ),
    )


def _req(**kw) -> Requirements:
    base = dict(destination="成都", date=TODAY, days=2)
    if "elders" in kw:
        base["travelers"] = Travelers(adults=1, elders=kw.pop("elders"))
    base.update(kw)
    return Requirements(**base)


def _finding(code: str, status: str, msg: str | None = None, **kw) -> SoftFinding:
    return SoftFinding(code=code, status=status, msg=msg, **kw)


# ══════════════════════════════════════════════════════════════
#  一、越界检查 —— 抓得住
# ══════════════════════════════════════════════════════════════

#: 每一对都是「模型最可能写出来的编造句 → 它违反了哪条铁律」。
#: ⚠️ 这些例句是**从"LLM 被判据 prompt 引导后最自然的写法"推出来的**，
#: 不是随手编的 —— 改 prompt 时如果模型开始说别的话，这张表要跟着更新。
OVERREACH_CASES = [
    ("武侯祠需要提前 3 天预约，否则进不去", "预约提前量"),
    ("旺季要提前七天订票", "预约提前量"),
    ("周末排队大约 2 小时", "排队时长"),
    ("每天限流 5000 人，去晚了就进不去", "限额"),
    ("门票 60 元，学生半价", "金额"),
    ("这里周一闭馆，别安排在那天", "闭馆日"),
    ("武侯祠需要预约", "预约断言"),
    ("这家店不预约就进不去", "预约断言"),
]


def test_overreach_patterns_catch_the_canonical_fabrications() -> None:
    """🔴 **这批测试就是 D45 那条闸门本身。**

    漏掉任意一条，就意味着那种编造的话会**直接进到 `Trip` 里**显示给用户，
    而项目的立身之本是"结果不是模型编的"。所以这 8 条必须一条不漏。
    """
    for msg, expected in OVERREACH_CASES:
        hits = find_overreach(msg)
        assert expected in hits, f"「{msg}」应该被判「{expected}」，实际只判出 {hits}"


# ══════════════════════════════════════════════════════════════
#  二、越界检查 —— 不误伤（同样重要）
# ══════════════════════════════════════════════════════════════

#: 合规的提醒。**数字全部来自输入**（步行量/车程/天数）或**只建议确认**（不构成断言）。
LEGIT_CASES = [
    # 数字来自我们自己算的事实 —— 这是软判据最值钱的一类输出
    "第 2 天步行 6.8 公里、车程 140 分钟，带两位长辈可能偏紧，可以考虑减少一站",
    "三天共 6 个站，节奏偏满",
    # 「建议确认」不构成断言 —— 这正是 needs_booking 唯一合规的写法
    "武侯祠是热门景点，又赶上周五，建议出发前确认一下是否需要预约",
    "建议提前确认预约政策",
    # 天气是我们给过的事实
    "第二天有雨，室内安排比较合适",
    # 空的
    None,
    "",
]


def test_overreach_check_does_not_kill_legitimate_reminders() -> None:
    """🔴 **这条和上一条同样重要。**

    闸门过严的后果不是"更安全"，是**软判据什么都不剩** ——
    而软判据的全部价值就是那几句提醒。特别是"建议确认预约政策"那句：
    它含"预约"两个字，如果检查器只会匹配关键词就会把**唯一合规的写法**拦掉。

    数字类同理：步行量 / 车程 / 天数是我们**自己算出来给它的**，
    引用它们不构成编造 —— 这是软判据里唯一"可验证"的那一半。
    """
    for msg in LEGIT_CASES:
        hits = find_overreach(msg)
        assert hits == [], f"「{msg}」是合规的，却被判成了 {hits}"


# ══════════════════════════════════════════════════════════════
#  三、层级：站 / 天 / 行程
# ══════════════════════════════════════════════════════════════


def test_findings_land_on_the_right_level() -> None:
    """4 条判据的主语不同，落点必须不同（`LEVEL_OF` 是唯一真源）。"""
    t = _trip(days=1)
    result = apply_soft_findings(
        t,
        [
            _finding(
                SoftCheckCode.NEEDS_BOOKING, CheckStatus.FAILED, "建议确认预约政策", day=1, seq=1
            ),
            _finding(
                SoftCheckCode.ELDER_FRIENDLY, CheckStatus.FAILED, "第 1 天步行 4.1 公里偏多", day=1
            ),
            _finding(
                SoftCheckCode.OVERALL_FEASIBLE, CheckStatus.PASSED
            ),
        ],
    )

    assert result.applied == 3
    assert [c.code for c in t.days[0].stops[0].checks] == ["needs_booking"]
    assert [c.code for c in t.days[0].checks] == ["elder_friendly"]
    assert [c.code for c in t.checks] == ["overall_feasible"]
    # 全部是软判据 —— 前端据此知道它们**不打回**
    assert all(c.level is CheckLevel.SOFT for c in t.days[0].stops[0].checks)
    assert all(c.level is CheckLevel.SOFT for c in t.checks)


def test_level_table_covers_every_code() -> None:
    """新增判据时忘了填 `LEVEL_OF` → 它会被静默丢弃。这里挡住那种静默。"""
    assert set(LEVEL_OF) == set(SoftCheckCode)


def test_model_cannot_choose_the_level() -> None:
    """🔴 模型把 `seq` 填到天级判据上 → **丢弃**，不是糊到某站上。

    糊上去的症状是"某天莫名多了一条别的地方的提醒"，
    而它不会报错、测试也不会红 —— 属于最难查的一类。
    """
    t = _trip(days=1)
    result = apply_soft_findings(
        t,
        [_finding(SoftCheckCode.ELDER_FRIENDLY, CheckStatus.FAILED, "偏紧", day=1, seq=1)],
    )
    # 天级判据给了 seq 也不影响落点（按 LEVEL_OF 走 day）
    assert [c.code for c in t.days[0].checks] == ["elder_friendly"]
    assert t.days[0].stops[0].checks == []


def test_finding_for_a_stop_that_does_not_exist_is_dropped() -> None:
    """指到不存在的天/站 → 丢弃并留原因，不许"就近挂一个"。"""
    t = _trip(days=1)
    result = apply_soft_findings(
        t,
        [
            _finding(SoftCheckCode.NEEDS_BOOKING, CheckStatus.FAILED, "建议确认", day=9, seq=1),
            _finding(SoftCheckCode.NEEDS_BOOKING, CheckStatus.FAILED, "建议确认", day=1, seq=9),
            _finding(SoftCheckCode.ELDER_FRIENDLY, CheckStatus.FAILED, "偏紧", day=9),
        ],
    )
    assert result.applied == 0
    assert len(result.dropped) == 3
    assert all("没有对应的位置" in d for d in result.dropped)


def test_trip_level_finding_ignores_day_and_seq() -> None:
    """⚠️ 行程级判据上带 `day` 是**无害的**（`LEVEL_OF` 说它挂 `Trip`，天/站无关）。

    这里刻意**和"指到不存在的天就丢弃"取不同的口径**，理由：

    | 情况 | 处理 | 为什么 |
    |---|---|---|
    | 站级判据指到不存在的站 | 丢弃 | 它本来要说"**那一站**怎么怎么样"，挂错地方 = 让用户去改一个不相干的站 |
    | 行程级判据多带了个 `day` | 忽略 | 它说的是"**整份行程**"，`day` 本来就是多余的噪音，丢掉它不损失任何信息 |

    一刀切地"看到不存在的 day 就丢"会把一条完全可用的整体节奏提醒白扔掉。
    """
    t = _trip(days=1)
    result = apply_soft_findings(
        t, [_finding(SoftCheckCode.OVERALL_FEASIBLE, CheckStatus.FAILED, "节奏偏满", day=7)]
    )
    assert result.applied == 1
    assert [c.code for c in t.checks] == ["overall_feasible"]


# ══════════════════════════════════════════════════════════════
#  四、丢弃规则
# ══════════════════════════════════════════════════════════════


def test_overreaching_finding_is_dropped_not_rewritten() -> None:
    """🔴 **整条丢弃，不是改写。**

    改写要我们自己造一句话 —— 那就等于**用代码编了一条提醒**，
    同样是编，只是编的人从模型换成了我们。丢弃是唯一诚实的选择：
    我们没断言任何事，只是少说一句。
    """
    t = _trip(days=1)
    result = apply_soft_findings(
        t,
        [
            _finding(
                SoftCheckCode.NEEDS_BOOKING,
                CheckStatus.FAILED,
                "武侯祠需要提前 3 天预约",
                day=1,
                seq=1,
            )
        ],
    )

    assert result.applied == 0
    assert t.days[0].stops[0].checks == [], "编造的话一句都不许进 Trip"
    assert len(result.dropped) == 1
    assert "预约提前量" in result.dropped[0]
    # ⚠️ 丢弃掉的**不算 warning** —— 它压根没成为一条判据
    assert result.warnings == 0
    assert t.summary.soft_warnings == 0


def test_fail_and_unknown_must_carry_a_msg() -> None:
    """没有 `msg` 的 fail 等于"告诉你这里有问题但不说什么问题" —— 用户没法行动。"""
    t = _trip(days=1)
    result = apply_soft_findings(
        t,
        [
            _finding(SoftCheckCode.ELDER_FRIENDLY, CheckStatus.FAILED, day=1),
            _finding(SoftCheckCode.OVERALL_FEASIBLE, CheckStatus.UNKNOWN),
        ],
    )
    assert result.applied == 0
    assert len(result.dropped) == 2
    assert all("没给 msg" in d for d in result.dropped)


def test_pass_may_omit_msg() -> None:
    """`pass` 不需要解释 —— 逼它说一句"没问题"只会制造废话。"""
    t = _trip(days=1)
    result = apply_soft_findings(t, [_finding(SoftCheckCode.OVERALL_FEASIBLE, CheckStatus.PASSED)])
    assert result.applied == 1
    assert t.checks[0].msg is None


def test_unknown_is_counted_separately_from_warnings() -> None:
    """三态要能分开数 —— `soft_warnings` 只数 `fail`。

    把 `unknown` 混进 warning 数里的后果：界面上的"3 个风险"里有 2 个其实是
    "我们不知道"，用户会按"有 3 个问题"去理解。
    """
    t = _trip(days=1)
    result = apply_soft_findings(
        t,
        [
            _finding(SoftCheckCode.ELDER_FRIENDLY, CheckStatus.FAILED, "偏紧", day=1),
            _finding(SoftCheckCode.OVERALL_FEASIBLE, CheckStatus.UNKNOWN, "没有日期，判不了旺季"),
        ],
    )
    assert (result.applied, result.warnings, result.unknown) == (2, 1, 1)
    assert t.summary.soft_warnings == 1


def test_recheck_is_idempotent() -> None:
    """🔴 跑两次不许翻倍。**这个 bug 只在"重新深度检查"路径上出现** ——
    首次生成永远看不出来，用户点了两次按钮才会发现每条提醒都变成了两条。

    ⚠️ **三层都要测。** 2026-09-16 变异验证发现：只测天级/站级时，
    把 `trip.checks = []` 那行删掉，测试**照样全绿** —— 因为天级/站级
    还有"按 `level` 过滤掉旧的软判据"这层兜底，而**行程级没有**。
    只测能过的那两层 = 那条清空语句等于没被测试覆盖。
    """
    t = _trip(days=1)
    findings = [
        _finding(SoftCheckCode.ELDER_FRIENDLY, CheckStatus.FAILED, "偏紧", day=1),
        _finding(SoftCheckCode.NEEDS_BOOKING, CheckStatus.FAILED, "建议确认", day=1, seq=1),
        _finding(SoftCheckCode.OVERALL_FEASIBLE, CheckStatus.FAILED, "三天 6 个站，节奏偏满"),
    ]

    apply_soft_findings(t, findings)
    apply_soft_findings(t, findings)

    assert len(t.days[0].checks) == 1, "天级判据翻倍了"
    assert len(t.days[0].stops[0].checks) == 1, "站级判据翻倍了"
    assert len(t.checks) == 1, "行程级判据翻倍了（这一层没有 level 过滤兜底）"
    assert t.summary.soft_warnings == 3


def test_idempotency_does_not_eat_hard_checks() -> None:
    """⚠️ 清空时**只能清软判据** —— 顺手把硬判据也清了，行程会变成"全绿"。"""
    from app.schemas import Check

    t = _trip(days=1)
    t.days[0].stops[0].checks.append(
        Check(code="poi_exists", level=CheckLevel.HARD, status=CheckStatus.PASSED)
    )
    apply_soft_findings(
        t, [_finding(SoftCheckCode.NEEDS_BOOKING, CheckStatus.FAILED, "建议确认", day=1, seq=1)]
    )

    codes = [c.code for c in t.days[0].stops[0].checks]
    assert "poi_exists" in codes, "硬判据被软判据的清空顺手删掉了"
    assert "needs_booking" in codes


# ══════════════════════════════════════════════════════════════
#  五、解析宽容 + 失败不拖垮流程
# ══════════════════════════════════════════════════════════════


def test_parser_accepts_bare_array_and_noisy_extra_keys() -> None:
    """模型偶尔给个裸数组、或每条多带一个 `reason`。**一个多余的键不该废掉四条判据。**"""
    findings, err = parse_soft_findings(
        '[{"code": "elder_friendly", "status": "fail", "msg": "偏紧", "day": 1, "reason": "x"}]'
    )
    assert len(findings) == 1
    assert err is None


def test_one_bad_finding_does_not_kill_the_others() -> None:
    """逐条宽容：坏的那条丢，好的留下。"""
    findings, err = parse_soft_findings(
        '{"findings": ['
        '{"code": "no_such_code", "status": "fail", "msg": "x"},'
        '{"code": "queue_time", "status": "fail", "msg": "周六人多，建议早到", "day": 1, "seq": 1}'
        "]}"
    )
    assert [f.code for f in findings] == [SoftCheckCode.QUEUE_TIME]
    assert err is not None and "解析失败" in err


def test_llm_crash_does_not_take_the_trip_down() -> None:
    """🔴 **软判据挂了，行程必须照样出来。**

    这一条是 `soft_check` 与 `generate_plan` 纪律**相反**的地方：
    生成节点失败要写 `state["error"]`（用户不能什么都拿不到），
    而软判据失败**绝不能**写 error —— 那会为了 4 句可有可无的提醒
    废掉一份本来完全可用的行程。
    """
    from fakes import ScriptedChatModel as _Scripted  # noqa: F401

    t = _trip(days=1)
    boom = _BoomLLM()

    result = run(run_soft_checks(t, _req(), boom))

    assert not result.ok
    assert "模型调用失败" in (result.error or "")
    assert t.summary.soft_warnings == 0
    assert t.checks == []
    assert all(d.checks == [] for d in t.days)


def test_good_output_after_a_bad_one_is_accepted() -> None:
    """第一次给脏 JSON，第二次给对的 → 应该用第二次的（重试一次是设计好的）。"""
    t = _trip(days=1)
    llm = ScriptedChatModel(
        script=[
            _ai("这不是 JSON"),
            soft_says(
                {
                    "code": "queue_time",
                    "status": "fail",
                    "msg": "周六中午人流通常较大，建议错峰",
                    "day": 1,
                    "seq": 1,
                }
            ),
        ]
    )
    result = run(run_soft_checks(t, _req(), llm))

    assert result.ok
    assert result.applied == 1
    assert llm.call_count == 2, "脏输出后应该重试一次"


def test_empty_findings_is_a_clean_no_op() -> None:
    """模型说"没什么要提醒的" → 一条都不写，且不算失败。"""
    t = _trip(days=1)
    result = run(run_soft_checks(t, _req(), ScriptedChatModel(script=[soft_says()])))
    assert result.ok
    assert result.applied == 0
    assert t.checks == []


def _ai(text: str):
    from langchain_core.messages import AIMessage

    return AIMessage(content=text)


class _BoomLLM:
    """一调就炸的 LLM。用来看"软判据挂了会不会拖垮行程"。"""

    def bind(self, **_kw):
        return self

    async def ainvoke(self, _messages):
        raise RuntimeError("上游 503")


# ══════════════════════════════════════════════════════════════
#  六、喂给模型的输入
# ══════════════════════════════════════════════════════════════


def test_prompt_carries_the_three_iron_rules() -> None:
    """三条铁律必须**在 prompt 里**，不能只写在 `soft.py` 的注释里 ——
    注释是给人看的，模型看不到。测试断言到**文案级**（同 `test_tools.py` 的做法）。
    """
    for phrase in ("不许出现任何无来源的具体数字", "建议确认", "只能用我给你的清单"):
        assert phrase in SOFT_SYSTEM_PROMPT, f"prompt 里少了铁律：{phrase}"


def test_facts_block_carries_only_facts() -> None:
    """🔴 喂进去的每个数字都必须是**我们算的**。

    这里每多写一个形容词，都是在引导模型复述它而不是解读它 ——
    而"复述事实"这件事模型做得很好，好到它会顺势编出没给过的数字。
    """
    text = build_soft_prompt(_trip(days=2), _req(elders=2))

    assert "成都" in text
    assert "武侯祠博物馆" in text
    assert "2 位长辈" in text        # 同行人结构
    assert "共 2 天" in text
    assert "步行 4.1 公里" in text    # 我们自己算的
    assert "车程 24 分钟" in text
    # 不该出现的：我们**没有**的信息
    for forbidden in ("门票", "预约", "排队", "热度"):
        assert forbidden not in text, f"输入里不该出现我们没有的信息：{forbidden}"


def test_facts_block_survives_an_empty_trip() -> None:
    """粘贴来的行程可能是空壳（没站点）—— 不许炸。"""
    t = _trip(days=0)
    text = build_soft_prompt(t, Requirements())
    assert "共 0 天" in text
