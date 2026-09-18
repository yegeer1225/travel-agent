"""M6 流映射测试：真图（假 LLM）跑 `graph_chat_stream`，断言 9 种 SSE 事件的映射。

不连 HTTP（那是 `test_chat_route.py` 的事）、不连库、不连 LLM。
这里盯的是**翻译规则**：LangGraph 内部事件 → 前端事件，顺序/形状/过滤是否正确。

🔴 最容易静默错的一处：**子 agent 内部的 search_poi 也会产生 tool 事件** ——
不过滤的话，前端轨迹卡会显示"正在搜索"×N，而模型明明只派了一次 task。
所以"只报顶层工具"这条断言是本文件的存在理由之一。
"""

from __future__ import annotations

import json
from datetime import date

from app.api.chat_stream import ChatHandle, NODE_LABELS, build_chat_input
from app.graph.graph import build_graph
from fakes import POI_IDS, SOFT_EMPTY, ai_text, ai_tool_call, make_nodes, run, soft_says, sub_finds
from test_graph import _draft, _intent, _search_all, _sub_finds_all

TODAY = date(2026, 9, 16)


def _happy_nodes(**kwargs):
    """与 test_graph 的 happy path 同构：一轮 task 铺满池子 → 收工 → 出稿。"""
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("soft_script", [SOFT_EMPTY])  # 允许调用方覆盖（如 check 测试喂非空 findings）
    return make_nodes(
        extract_script=_intent(),
        tool_script=[_search_all(), ai_text("信息够了，我打算这么排。")],
        plan_script=[ai_text(_draft())],
        sub_script=_sub_finds_all(),
        **kwargs,
    )


async def _collect(nodes, message: str = "去成都玩3天"):
    graph = build_graph(nodes)
    handle = ChatHandle(graph, "s1")
    events, final = [], None
    async for item in handle.stream(message):
        if "final_state" in item:
            final = item["final_state"]
        elif "event" in item:
            events.append(item["event"])
    return events, final


def _types(events) -> list[str]:
    return [e.type for e in events]


# ══════════════════════════════════════════════════════════════
#  正常路径
# ══════════════════════════════════════════════════════════════


def test_happy_path_emits_full_event_sequence():
    events, final = run(_collect(_happy_nodes()))

    assert events[0].type == "node" and events[0].node == "parse_intent"
    assert events[0].phase == "start" and events[0].label == "正在理解你的需求"
    # 结尾三连：总结 token → trip（trip 是最后一个事件；done 由路由补）
    assert _types(events)[-2:] == ["token", "trip"]
    assert "error" not in _types(events), f"不该有 error：{[e for e in events if e.type == 'error']}"
    assert final is not None and final.get("trip"), "终态里要有行程"


def test_skeleton_event_arrives_before_tool_events():
    """🔴 D77 首结果延迟：骨架事件必须**先于**任何工具事件到达 ——
    它存在的意义就是"在搜索循环还在跑的时候先给用户看个框架"。
    排在 tool_call 之后 = 这个节点白加了。"""
    events, _ = run(_collect(_happy_nodes()))

    types = _types(events)
    assert "skeleton" in types, "happy path 必须发骨架事件"
    skel_idx = types.index("skeleton")
    first_tool_idx = types.index("tool_call")
    assert skel_idx < first_tool_idx, f"骨架（第 {skel_idx} 个事件）必须先于工具事件（第 {first_tool_idx} 个）"

    skel = events[skel_idx]
    assert skel.title and skel.days, "骨架要带标题和天列表"
    assert skel.note, "必须带'未经验证'提示 —— 这是骨架和正式行程的区别声明"
    for d in skel.days:
        assert d.day >= 1 and isinstance(d.stops, list)
        # 骨架 stops 是地名文本，不可能是候选池里的真实 id
        assert all(s for s in d.stops)


def test_skeleton_not_emitted_on_ask_more_path():
    """需求不齐走追问时**不发骨架** —— 没有目的地/日期的骨架只会误导用户。"""
    events, _ = run(_collect(make_nodes(today=TODAY, extract_script=_intent(script_date=None))))

    assert "skeleton" not in _types(events)
    assert "trip" not in _types(events)
    assert "token" in _types(events)  # 追问文本照发


def test_tool_events_report_only_top_level_task():
    """🔴 子 agent 内部的 search_poi 不得漏进 tool 事件 —— 只报模型发起的 task。"""
    events, _ = run(_collect(_happy_nodes()))

    calls = [e for e in events if e.type == "tool_call"]
    results = [e for e in events if e.type == "tool_result"]
    assert len(calls) == 1, f"只该有一次顶层工具调用，实际 {[ (e.tool, e.label) for e in calls ]}"
    assert calls[0].tool == "task"
    assert calls[0].label == "正在收集候选：成都值得去的地点"
    assert not any(e.tool == "search_poi" for e in calls + results), "子 agent 内部搜索泄漏到了 tool 事件"
    # call_id 配对
    assert {c.call_id for c in calls} == {r.call_id for r in results}
    assert results[0].ok is True and results[0].summary


def test_check_event_carries_tri_state_card():
    # 🔴 soft 给**非零** warnings：`soft_report.warnings` 是 int 不是 list ——
    # 端到端真跑时曾因 `len(int)` 炸（SOFT_EMPTY 的 0 恰好让两种写法都过，测不出来）
    nodes = _happy_nodes(soft_script=[soft_says(
        {"code": "needs_booking", "status": "fail", "msg": "建议提前确认预约政策", "day": 1, "seq": 1},
    )])
    events, _ = run(_collect(nodes))
    checks = [e for e in events if e.type == "check"]
    assert len(checks) == 1, "正常路径（无打回）只校验一轮"
    evt = checks[0]
    assert evt.round == 1
    assert evt.hard_errors == 0
    assert evt.soft_warnings == 1, "软提醒条数 = SoftReport.warnings（int）"
    assert isinstance(evt.checks, list) and evt.checks, "三态卡的原料（判据列表）不能是空的"


def test_nodes_all_carry_chinese_labels():
    events, _ = run(_collect(_happy_nodes()))
    node_events = [e for e in events if e.type == "node"]
    assert node_events, "节点事件一个都没有 = 映射完全失效"
    for e in node_events:
        assert e.label == NODE_LABELS[e.node], f"{e.node} 的 label 必须来自后端映射"
        if e.phase == "end":
            assert e.elapsed_ms is not None, "end 帧要带耗时"


# ══════════════════════════════════════════════════════════════
#  追问路径（必问项缺口）
# ══════════════════════════════════════════════════════════════


def test_ask_more_path_streams_token_not_trip():
    nodes = make_nodes(
        today=TODAY,
        extract_script=[ai_text(json.dumps({"destination": "成都"}, ensure_ascii=False))],
        tool_script=[ai_text("够了")],
        plan_script=[ai_text(_draft())],
        sub_script=[],
        soft_script=[SOFT_EMPTY],
    )
    events, final = run(_collect(nodes, message="下周去成都"))

    tokens = [e.text for e in events if e.type == "token"]
    assert tokens and "".join(tokens).strip(), "追问文本要以 token 发出"
    assert not any(e.type == "trip" for e in events), "缺日期时不会有行程"
    assert final is not None and final.get("ask"), "终态里要有追问文本"
    assert "error" not in _types(events)


# ══════════════════════════════════════════════════════════════
#  输入构造（多轮合并语义 / 断线恢复）
# ══════════════════════════════════════════════════════════════


def test_build_chat_input_continuation_keeps_checkpoint_facts():
    """🔴 续轮输入**不带** collected_pois / requirements —— 给了（哪怕空 dict）就把
    第一轮攒下的候选池和目的地清掉了。这是 LangGraph 合并语义的坑。"""
    cont = build_chat_input("加个博物馆", session_id="s1", resume=False, has_checkpoint=True)
    assert "collected_pois" not in cont
    assert "requirements" not in cont
    assert "missing_required" not in cont
    assert "subagent_trace" not in cont
    assert cont["user_message"] == "加个博物馆"
    assert cont["tool_call_count"] == 0, "每轮计数从零开始（预算是每轮的）"

    first = build_chat_input("去成都", session_id="s1", resume=False, has_checkpoint=False)
    assert "collected_pois" in first, "首轮要有完整初始 state（池子快照落点等）"


def test_build_chat_input_resume_returns_none():
    """D27：断线恢复时 input=None —— LangGraph 的语义是"从 checkpoint 停下处继续"，
    绝不重发用户消息（否则消息重复落库、agent 重跑一遍扣两次钱）。"""
    assert build_chat_input("去成都", session_id="s1", resume=True, has_checkpoint=False) is None
