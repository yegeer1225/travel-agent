"""CLI 输出格式的守卫 —— 只测**渲染出来的行**，不测业务流程。

═══════════════════════════════════════════════════════════════
 为什么 CLI 也值得有测试
═══════════════════════════════════════════════════════════════

`run_cli.py` 是这个项目**唯一能亲眼看到 agent 过程**的地方 ——
判据三态、打回轮数、软判据拦下了几条，全在这里。它的输出如果错位，
读的人会把一条提醒算到**另一个站**头上，而这**不会报错、也不会让任何测试红**。

真实教训（2026-09-16）：`_fmt_stop` 里调 `_print_issues` 时**直接 print**，
但它自己返回的字符串是调用方**之后**才打印的 —— 于是每条判据都印在了
**上一个站**的下面。这个 bug 从 M3 就存在，只因为那时非 pass 的判据很少
（一个 3 天行程就 1 条），一直没被看出来；软判据一上，每个站都有提醒，
错位立刻暴露成"每条提醒都挂错了地方"。

教训：**"有副作用的格式化函数"是一类隐形 bug**，
  而它的症状是"读起来有点怪"，不会有人当 bug 报。
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_cli  # noqa: E402


def _stop(**overrides) -> dict:
    base = {
        "seq": 1,
        "name": "武侯祠博物馆",
        "poi_id": "B001C07VJ2",
        "arrive": "09:00",
        "leave": "11:00",
        "stay_min": 120,
        "checks": [],
    }
    base.update(overrides)
    return base


def test_fmt_stop_has_no_side_effect_printing() -> None:
    """🔴 `_fmt_stop` **必须只返回字符串**，不许顺手 print。

    它返回的多行文本由调用方统一打印；在这里 print 会让那几行
    **先于**这一站的标题出现 —— 读起来就是"提醒挂在上一个站下面"。
    """
    stop = _stop(
        checks=[
            {
                "code": "needs_booking",
                "level": "soft",
                "status": "fail",
                "msg": "这是热门景点，建议提前确认预约政策",
            }
        ]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        text = run_cli._fmt_stop(stop)

    assert buf.getvalue() == "", f"`_fmt_stop` 打印了东西：{buf.getvalue()!r}"
    assert "↳" in text, "判据的说明行应该被包含在返回值里"


def test_issue_line_comes_after_its_own_stop() -> None:
    """判据的说明行必须排在**它自己那一站**的标题之后。"""
    text = run_cli._fmt_stop(
        _stop(
            checks=[
                {
                    "code": "open_today",
                    "level": "hard",
                    "status": "unknown",
                    "msg": "没有营业时间数据，无法判断",
                }
            ]
        )
    )
    lines = text.splitlines()
    name_at = next(i for i, ln in enumerate(lines) if "武侯祠博物馆" in ln)
    arrow_at = next(i for i, ln in enumerate(lines) if "↳" in ln)

    assert name_at < arrow_at, f"判据行跑到了站名之前：\n{text}"


def test_pass_checks_are_not_printed_as_issues() -> None:
    """`pass` 不印说明 —— 印了会把输出淹掉（一行 4 个 ✅ 加 4 行废话）。"""
    text = run_cli._fmt_stop(
        _stop(
            checks=[
                {"code": "poi_exists", "level": "hard", "status": "pass", "msg": None},
                {"code": "reachable", "level": "hard", "status": "pass", "msg": "车程很轻松"},
            ]
        )
    )
    assert "↳" not in text


def test_soft_and_hard_failures_get_different_icons() -> None:
    """🔴 软判据 `fail` 是 🔔 不是 🔴。

    两者都是"红"的后果：一份 3 天行程会有 3~5 条软提醒 + 0 条硬错，
    全用红标会让人以为"这条行程有 5 个严重问题"，而其中多数只是
    "建议提前确认预约政策"。**图标是这一堆信息里唯一的分级信号。**
    """
    soft = run_cli._fmt_checks([{"code": "queue_time", "level": "soft", "status": "fail"}])
    hard = run_cli._fmt_checks([{"code": "reachable", "level": "hard", "status": "fail"}])
    assert soft == "queue_time🔔"
    assert hard == "reachable🔴"


def test_unknown_keeps_its_own_icon_in_both_levels() -> None:
    """`unknown` 在两层的图标都是 ⚪ —— 它既不是通过也不是失败。"""
    assert "⚪" in run_cli._fmt_checks([{"code": "open_today", "level": "hard", "status": "unknown"}])
    assert "⚪" in run_cli._fmt_checks(
        [{"code": "needs_booking", "level": "soft", "status": "unknown"}]
    )


@pytest.mark.parametrize("status", ["pass", "fail", "unknown"])
def test_every_status_has_an_icon(status: str) -> None:
    """三态任何一个漏了图标就会显示成 `?` —— 那不是"知道它坏了"，是"看不出"。

    `run_cli` 里写错过一次同类的东西（把天气状态字面量写成 `"available"`），
    后果是**永远显示"无法判定"**，一个不报错的静默错误。
    """
    out = run_cli._fmt_checks([{"code": "x", "level": "hard", "status": status}])
    assert out == f"x{run_cli._CHECK_ICON[status]}"
    assert "?" not in out


# ══════════════════════════════════════════════════════════════
#  P1（D79）后子 agent trace 已退役 —— 原三个 _fmt_trace 测试随之删除；
#  run_cli 改印台账（searched_keywords）。
# ══════════════════════════════════════════════════════════════


def test_print_result_shows_searched_keywords(capsys) -> None:
    """台账是"搜索花了多少功夫"现在唯一可见的地方，必须印出来。"""
    state = {
        "requirements": {"destination": "成都"},
        "searched_keywords": ["成都|武侯祠", "成都|火锅"],
    }
    run_cli.print_result(state)
    out = capsys.readouterr().out
    assert "【已搜关键词】2 个" in out
    assert "成都|武侯祠" in out

