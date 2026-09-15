"""需求抽取层自检。

盯三件事：
1. **「必问 7 项」和「阻塞 2 项」的边界不能糊** —— 糊了 agent 会卡在预算上追问个没完
2. **模型的"等于没说"要能被收敛** —— `""` / `"无"` / `"未知"` 必须变成 `None`，
   否则"目的地 = 无"会被当成一个有效目的地
3. **合并是"新的非空覆盖旧的"** —— 不能整体替换，否则用户第二句只说"改成 4 天"
   就把目的地抹掉了，而且没人会发现
"""

from __future__ import annotations

from datetime import date

import pytest

from app.graph.intent import (
    BLOCKING_FIELDS,
    REQUIRED_FIELDS,
    Requirements,
    Travelers,
    build_ask_text,
    build_intent_prompt,
    merge_requirements,
    parse_intent_json,
)

# ══════════════════════════════════════════════════════════════
#  常量集合的边界（A25）
# ══════════════════════════════════════════════════════════════


def test_required_is_seven():
    assert len(REQUIRED_FIELDS) == 7


def test_blocking_is_two_and_subset_of_required():
    assert BLOCKING_FIELDS == ("destination", "date")
    assert set(BLOCKING_FIELDS) < set(REQUIRED_FIELDS), "阻塞项必须是必问项的真子集"


def test_missing_blocking_ignores_non_blocking_fields():
    """只缺天数/预算这类非阻塞项时，**不该拦流程**。"""
    req = Requirements(destination="成都", date=date(2026, 10, 1))
    assert req.missing_blocking() == []


def test_missing_blocking_reports_only_destination_and_date():
    req = Requirements()
    assert req.missing_blocking() == ["destination", "date"]
    # 顺序稳定（tuple 不是 set）—— 追问文案按这个顺序排
    assert req.missing_blocking() == list(BLOCKING_FIELDS)


# ══════════════════════════════════════════════════════════════
#  「等于没说」的收敛
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "raw",
    ["", "  ", "null", "NULL", "None", "无", "未知", "未提及", "不清楚", "不知道", "N/A"],
)
def test_placeholder_values_become_none(raw):
    """模型很爱把"没有"写成各种占位词，**都必须收敛成 `None`**。"""
    req, err = parse_intent_json({"destination": raw, "date": None})
    assert err is None
    assert req is not None
    assert req.destination is None


def test_empty_list_and_dict_also_count_as_missing():
    req, _ = parse_intent_json({"preferences": [], "travelers": {}})
    assert req is not None
    assert req.preferences == []
    assert req.travelers is None  # ← `{}` 被收敛成 None，不是"一个有 0 人的同行团"


# ══════════════════════════════════════════════════════════════
#  解析容错
# ══════════════════════════════════════════════════════════════


def test_parse_strips_markdown_fence():
    raw = '```json\n{"destination": "成都", "days": 3}\n```'
    req, err = parse_intent_json(raw)
    assert err is None
    assert req is not None
    assert req.destination == "成都"
    assert req.days == 3


def test_parse_rejects_non_json():
    req, err = parse_intent_json("我觉得成都不错")
    assert req is None
    assert err is not None and "不是合法 JSON" in err


def test_parse_rejects_json_array():
    req, err = parse_intent_json("[1, 2, 3]")
    assert req is None
    assert err is not None and "JSON 对象" in err


def test_parse_ignores_unknown_fields():
    """`extra="ignore"` 的兑现：模型顺手多写一个字段不该让整个流程失败。

    （和 `schemas.py` 的 `forbid` 相反 —— 理由见 `intent.py` 的模块 docstring。）
    """
    req, err = parse_intent_json({"destination": "成都", "confidence": 0.93, "备注": "x"})
    assert err is None
    assert req is not None
    assert req.destination == "成都"
    assert not hasattr(req, "confidence")


def test_unparsable_field_is_dropped_not_fatal():
    """🔴 **一个字段读不了，只丢那一个字段。**

    2026-09-16 真实跑出来的场景：模型返回
    `{"travelers": {"adults": null, "children": 0, "elders": 0}}`
    （用户说的是"带爸妈"），而 `adults` 当时声明的是非空 `int` →
    校验失败 → **整条抽取被丢掉**，连已经抽对的目的地和日期一起没了。
    用户看到的是一句"还差两个必填项，去哪座城市？" —— 而他刚说了成都。

    `extra="ignore"` 的初衷就是"宽容"，但"一个字段类型错废掉整条"与之直接矛盾。
    """
    req, err = parse_intent_json({"destination": "成都", "days": "三天"})
    assert err is None
    assert req is not None
    assert req.destination == "成都", "抽对的字段必须留下"
    assert req.days is None, "读不了的字段变 None —— **不是被猜成 3**"


def test_soft_int_keeps_unambiguous_numbers():
    """宽容解析只做**没有歧义**的转换：去掉粘在数字上的单位。"""
    req, _ = parse_intent_json({"days": "3天", "budget": "1500元"})
    assert req is not None
    assert req.days == 3
    assert req.budget == 1500


def test_soft_int_refuses_chinese_numerals():
    """中文数字**不转** —— 那是猜。"五天"→ None，宁可走默认值。"""
    req, _ = parse_intent_json({"days": "五天"})
    assert req is not None
    assert req.days is None


def test_soft_int_rejects_bool():
    """`True` 在 Python 里是 `int` 的子类，不显式排除的话 `{"days": true}` 会静默变 1 天。"""
    req, _ = parse_intent_json({"days": True})
    assert req is not None
    assert req.days is None


def test_unparsable_date_becomes_none():
    """"下周三"没被换算时丢掉该字段，而不是报错废掉整条。"""
    req, err = parse_intent_json({"destination": "成都", "date": "下周三"})
    assert err is None
    assert req is not None
    assert req.destination == "成都"
    assert req.date is None
    assert req.missing_blocking() == ["date"], "丢掉后自然变成『缺日期 → 追问』"


def test_partial_travelers_is_kept():
    """`{"adults": null, "elders": 2}` 是**有信息**的（两位老人），必须留下。"""
    req, err = parse_intent_json({"travelers": {"adults": None, "elders": 2}})
    assert err is None
    assert req is not None
    assert req.travelers is not None
    assert req.travelers.elders == 2
    assert req.travelers.adults is None


def test_parse_still_reports_errors_it_cannot_fix():
    """宽容不等于不报错 —— `notes` 拿到一个对象，那是没法修的。"""
    req, err = parse_intent_json({"notes": {"why": "说不清"}})
    assert req is None
    assert err is not None and "notes" in err


def test_parse_accepts_iso_date_and_travelers():
    req, err = parse_intent_json(
        {
            "destination": "成都",
            "date": "2026-10-01",
            "travelers": {"adults": 2, "elders": 1},
        }
    )
    assert err is None
    assert req is not None
    assert req.date == date(2026, 10, 1)
    assert req.travelers is not None and req.travelers.elders == 1


# ══════════════════════════════════════════════════════════════
#  合并
# ══════════════════════════════════════════════════════════════


def test_merge_fills_new_field():
    old = Requirements(destination="成都", date=date(2026, 10, 1))
    new = Requirements(days=4)
    merged = merge_requirements(old, new)
    assert merged.destination == "成都"
    assert merged.days == 4


def test_merge_does_not_erase_known_values():
    """🔴 本文件最重要的一条：用户只说"改成 4 天"，目的地不能被抹掉。"""
    old = Requirements(
        destination="成都",
        date=date(2026, 10, 1),
        travelers=Travelers(adults=2, elders=1),
    )
    new = Requirements(days=4)  # 其余全是 None
    merged = merge_requirements(old, new)
    assert merged.destination == "成都"
    assert merged.date == date(2026, 10, 1)
    assert merged.travelers is not None and merged.travelers.elders == 1


def test_merge_overwrites_when_new_value_present():
    old = Requirements(destination="成都", days=3)
    new = Requirements(destination="重庆")
    merged = merge_requirements(old, new)
    assert merged.destination == "重庆"
    assert merged.days == 3  # 没提到的保持


def test_merge_does_not_let_placeholder_overwrite():
    """模型把"这一轮没提到"写成 `"无"` 时，**不能覆盖**上去。"""
    old = Requirements(destination="成都", notes="不吃辣")
    new, _ = parse_intent_json({"destination": "无", "notes": "未提及"})
    assert new is not None
    merged = merge_requirements(old, new)
    assert merged.destination == "成都"
    assert merged.notes == "不吃辣"


# ══════════════════════════════════════════════════════════════
#  缺省值
# ══════════════════════════════════════════════════════════════


def test_with_defaults_fills_non_blocking_only():
    req = Requirements(destination="成都", date=date(2026, 10, 1)).with_defaults()
    assert req.days == 3
    assert req.preferences == ["综合"]
    assert req.travelers is not None and req.travelers.adults == 2


def test_with_defaults_does_not_invent_blocking_fields():
    """🔴 缺省值**不能**填 destination / date ——
    否则 `missing_blocking()` 永远为空，"追问"这条路就静默失效了。"""
    req = Requirements().with_defaults()
    assert req.destination is None
    assert req.date is None
    assert req.missing_blocking() == ["destination", "date"]


def test_with_defaults_keeps_user_values():
    req = Requirements(
        destination="成都",
        date=date(2026, 10, 1),
        days=5,
        preferences=["摄影"],
    ).with_defaults()
    assert req.days == 5
    assert req.preferences == ["摄影"]


# ══════════════════════════════════════════════════════════════
#  追问文案与 prompt 锚点
# ══════════════════════════════════════════════════════════════


def test_ask_text_lists_every_missing_field():
    text = build_ask_text(Requirements(), ["destination", "date"])
    assert "去哪座城市" in text
    assert "哪天出发" in text
    # 缺两个必填项时应该安抚一句"其余不说也行"，否则用户以为要被盘问 7 轮
    assert "默认" in text


def test_ask_text_acknowledges_known_destination():
    req = Requirements(destination="成都")
    text = build_ask_text(req, ["date"])
    assert "成都" in text


def test_intent_prompt_contains_today_anchor_and_weekday():
    """🔴 锚点日期必须显式给出 —— 不给的话模型会以训练时间为基准算"下周三"，
    而算错的产物是一个**完全合法的日期**，下游没有任何环节能发现。"""
    prompt = build_intent_prompt("下周三去成都", today=date(2026, 9, 15), weekday="周二")
    assert "2026-09-15" in prompt
    assert "周二" in prompt


def test_intent_prompt_carries_previous_round():
    prompt = build_intent_prompt(
        "改成4天",
        today=date(2026, 9, 15),
        weekday="周二",
        existing={"destination": "成都", "date": "2026-10-01"},
    )
    assert "成都" in prompt
    assert "保持原样" in prompt, "要明确告诉模型：没提到的原样带回，否则它会当成没这回事"


def test_intent_prompt_drops_placeholder_from_history():
    """历史里的"等于没说"不该被当成已知信息展示 —— 那会诱导模型接着编。"""
    prompt = build_intent_prompt(
        "改成4天",
        today=date(2026, 9, 15),
        weekday="周二",
        existing={"destination": "无", "days": 3},
    )
    assert '"destination"' not in prompt
    assert '"days"' in prompt
