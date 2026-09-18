"""图结构自检 —— **三层循环到底跑不跑得通**。

这个文件和 `test_nodes.py` 的分工：

| 文件 | 坏了的表现 |
|---|---|
| `test_nodes.py` | 某一步算错了、该写的 state 没写 |
| **本文件** | **该走的路没走 / 绕圈绕不出去 / 该刹车没刹 / 该回环没回** |

`test_nodes.py` 全绿但本文件红，说明**节点各自是对的，接线错了** ——
这是最容易发生、也最该被独立测出来的一类问题。

═══════════════════════════════════════════════════════════════
 用假 LLM 也能测的三条"真"东西
═══════════════════════════════════════════════════════════════

1. **L2 回环真的回到 `agent_step`**（不是回到生成节点）
2. **L3 打回真的只回 L2，且打满 2 轮就收手**（不会无限重排）
3. **每个 `tool_call` 都拿到了回应**（这是真实 400 的代理断言 ——
   真模型下漏回应会 400，假模型下不会，所以要用"数量 + id 对齐"来断言）

═══════════════════════════════════════════════════════════════
 一个写测试时才暴露出来的 mock 缺陷（2026-09-16）
═══════════════════════════════════════════════════════════════

这批测试第一次跑就 13 条全红，原因不是图接错了，是
**mock 的 `search_poi` 只比名称/别名，模型用类别词（"古迹"/"美食"）搜必然空手** ——
而 prompt 恰恰引导它这么做。池子空 → `generate_plan` 直接拦住 → 全链路失败。

修法是给 mock 补一张**检索标签表**（`_MOCK_SEARCH_TAGS`），
把它的检索行为放宽到接近真实高德的全文检索。

⚠️ 教训：**mock 的"数据保真"和"行为保真"是两件事。**
数据字段不能编（没有就留 `None`），但**行为必须像真的** ——
一个"数据诚实、行为失真"的 mock 会让所有端到端测试红得毫无线索。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from app.graph.graph import RECURSION_LIMIT, build_graph, initial_state
from app.graph.nodes import FORCE_STOP_TEXT, TOOL_BUDGET_TEXT
from app.providers.base import DistanceResult
from app.providers.mock import MockAmapProvider
from fakes import (
    POI_IDS,
    BoomChatModel,
    ai_multi_tool_calls,
    ai_text,
    ai_tool_call,
    make_nodes,
    run,
    soft_says,
)

TODAY = date(2026, 9, 16)
FAKE_ID = "B0000_THIS_IS_MADE_UP"


def _nodes(**kwargs):
    """固定"今天"的 `make_nodes`（理由见 `test_nodes.py` 同名函数）。"""
    kwargs.setdefault("today", TODAY)
    return make_nodes(**kwargs)


async def _run(message: str, *, nodes, pending=None) -> dict:
    graph = build_graph(nodes)
    return await graph.ainvoke(
        initial_state(message, session_id="s1", pending_messages=pending),
        {"recursion_limit": RECURSION_LIMIT},
    )


def _intent(script_date: str | None = TODAY.isoformat(), **extra) -> list:
    payload = {
        "destination": "成都",
        "date": script_date,
        "days": 3,
        "travelers": {"adults": 2, "elders": 1},
        "preferences": ["历史古迹", "美食"],
        **extra,
    }
    return [ai_text(json.dumps(payload, ensure_ascii=False))]


def _search_all():
    """主 agent 直搜一次，把 mock 池铺满。

    🔴 为什么要"铺满"而不是随便搜一个词：`_draft()` 默认用 `POI_IDS[:3]`，
    如果池子里缺其中一个，测试就会**意外走进 repair 路径** ——
    那时它测的就不是"正常路径"了，而断言失败的信息完全指不到这里。
    显式铺满 = 让每条测试测它自己声称要测的东西。

    P1（D79）后主 agent 直搜 `search_poi`。关键词「景点」在 mock 的检索
    标签表里 9 个 POI **全命中**（每个都有这个 tag），一次搜索就够铺满
    （池子记的是执行层全量，不受展示层 top-8 截断影响）。
    """
    return ai_tool_call("search_poi", {"keyword": "景点", "city": "成都"})


def _draft(*poi_ids: str, days: int = 1, title: str = "成都三日") -> str:
    """造一份合法的草稿 JSON。id 不够时循环复用。"""
    ids = list(poi_ids) or POI_IDS[:3]
    day_list = []
    for d in range(days):
        picks = [ids[(d * 2 + i) % len(ids)] for i in range(2)]
        day_list.append(
            {
                "date": (TODAY + timedelta(days=d)).isoformat(),
                "theme": f"第{d + 1}天",
                "stops": [
                    {
                        "poi_id": pid,
                        "arrive": f"{9 + i * 2:02d}:00",
                        "stay_min": 90,
                        "match_reason": "测试理由",
                    }
                    for i, pid in enumerate(picks)
                ],
            }
        )
    return json.dumps({"title": title, "days": day_list}, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════
#  形状
# ══════════════════════════════════════════════════════════════


def test_graph_compiles_with_eight_nodes():
    graph = build_graph(_nodes())
    names = set(graph.get_graph().nodes)
    assert {
        "parse_intent",
        "ask_more",
        "agent_step",
        "tool_step",
        "generate_plan",
        "check_plan",
        "repair",
        "render",
    } <= names


def test_tool_step_has_back_edge_to_agent_step():
    """L2 的回边必须在。少了它，工具调用完图就断了。"""
    graph = build_graph(_nodes())
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    assert ("tool_step", "agent_step") in edges


def test_repair_edge_goes_back_to_l2_not_generate_plan():
    """🔴 L3 的回边目标是 **`agent_step`**，不是 `generate_plan`。

    回生成节点的话，池子没变、模型在同一份清单里再挑一次，
    **没有任何新信息注入**；回 L2 才能重新搜、把池子变大。
    这条连线就是"重排"和"重查"合一的地方。
    """
    graph = build_graph(_nodes())
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    assert ("repair", "agent_step") in edges
    assert ("repair", "generate_plan") not in edges


def test_initial_state_carries_every_budget_counter():
    """🔴 `TripState` 是 `total=False`，**漏字段不报错**，
    只会让对应的守卫静默失效（比如漏了 `tool_call_count`，上限永远不触发）。

    这类 bug 不会崩，只会悄悄多花钱 —— 所以要在这里钉住。
    """
    state = initial_state("去成都", session_id="s1")
    for key in ("tool_call_count", "agent_rounds", "check_rounds"):
        assert state[key] == 0, f"预算计数器 {key} 必须在初始 state 里"
    assert state["collected_pois"] == {}
    assert state["pending_messages"] == []
    assert state["messages"], "用户这句话要作为第一条消息进上下文"


# ══════════════════════════════════════════════════════════════
#  ⑧ ask_more：缺阻塞项就停下
# ══════════════════════════════════════════════════════════════


def test_missing_date_stops_at_ask_more():
    nodes = _nodes(
        extract_script=_intent(script_date=None),
        tool_script=[_search_all()],
        plan_script=[ai_text(_draft())],
    )
    state = run(_run("去成都玩几天", nodes=nodes))

    assert state.get("ask"), "应该生成追问文案"
    assert "哪天出发" in state["ask"]
    assert not state.get("trip"), "缺阻塞项时不该出行程"
    assert nodes.llm_tool.call_count == 0, "还没开工，不该进工具循环"
    assert nodes.llm_plan.call_count == 0


def test_missing_destination_stops_at_ask_more():
    nodes = _nodes(
        extract_script=_intent(destination=None),
        tool_script=[_search_all()],
        plan_script=[ai_text(_draft())],
    )
    state = run(_run("帮我排个三天行程", nodes=nodes))

    assert "去哪座城市" in state["ask"]
    assert not state.get("trip")
    assert nodes.llm_tool.call_count == 0


def test_past_date_also_stops_at_ask_more():
    """抽到过去的日期 → 丢弃 → 变成"缺日期" → 追问（而不是排出一个过去的行程）。"""
    nodes = _nodes(
        extract_script=_intent(script_date="2026-01-01"),
        tool_script=[_search_all()],
        plan_script=[ai_text(_draft())],
    )
    state = run(_run("去成都", nodes=nodes))

    assert "哪天出发" in state.get("ask", "")
    assert state["requirements"]["destination"] == "成都", "目的地不该连带丢掉"
    assert not state.get("trip")
    assert nodes.llm_tool.call_count == 0


# ══════════════════════════════════════════════════════════════
#  正常路径
# ══════════════════════════════════════════════════════════════


def _happy_nodes(**kwargs):
    return _nodes(
        extract_script=_intent(),
        tool_script=[_search_all(), ai_text("信息够了，我打算这么排。")],
        plan_script=[ai_text(_draft(days=3))],
        **kwargs,
    )


def test_happy_path_produces_three_day_trip():
    nodes = _happy_nodes()
    state = run(_run("下周三去成都玩3天，带爸妈", nodes=nodes))

    assert not state.get("error"), state.get("error")
    assert not state.get("ask")
    trip = state["trip"]
    assert len(trip["days"]) == 3
    assert trip["destination"] == "成都"
    assert trip["source"] == "generated"
    assert trip["session_id"] == "s1"

    # 汇总数字是代码算的：3 天 × 2 站
    assert trip["summary"]["stop_count"] == 6
    assert trip["summary"]["total_distance_km"] > 0
    assert trip["summary"]["hard_errors"] == 0

    # 过程中的计量：直搜计 1 次工具调用
    assert state["tool_call_count"] == 1
    assert state["agent_rounds"] == 2
    assert state["check_rounds"] == 0

    # D71 台账：搜过什么词有账可查（P1 后内联在 tool_step）
    assert state["searched_keywords"] == ["成都|景点"]

    # 每一站都带判据（前端三态上色要用），且全部通过
    for day in trip["days"]:
        assert day["day_stats"]["distance_km"] >= 0
        for stop in day["stops"]:
            assert stop["checks"][0]["code"] == "poi_exists"
            assert stop["checks"][0]["status"] == "pass"


def test_weather_is_collected_by_code_for_each_day():
    """天气由代码查（不靠模型调工具）—— 3 天就该有 3 条。"""
    nodes = _happy_nodes()
    state = run(_run("去成都3天", nodes=nodes))

    assert len(state["collected_weather"]) == 3
    for day in state["trip"]["days"]:
        assert day["weather"] is not None
        assert day["weather"]["status"] == "ok"


def test_every_tool_call_gets_a_response_in_real_graph():
    """🔴 真实 400 的**代理断言**。

    真模型下"漏回应 tool_call"会稳定 400；假模型不会报错，
    所以这里改成断言"每个 tool_call id 都有对应的 ToolMessage"。
    没有这条，`tool_step` 哪天被改成只处理 `calls[0]`，测试会全绿。
    """
    nodes = _nodes(
        extract_script=_intent(),
        tool_script=[
            _search_all(),
            ai_multi_tool_calls(
                ("calc_distance", {"from_poi_id": POI_IDS[0], "to_poi_id": POI_IDS[1]}),
                ("get_weather", {"date_str": TODAY.isoformat()}),
            ),
            ai_text("够了"),
        ],
        plan_script=[ai_text(_draft())],
    )
    state = run(_run("去成都", nodes=nodes))

    call_ids: list[str] = []
    answered: list[str] = []
    for msg in state["messages"]:
        for call in getattr(msg, "tool_calls", None) or []:
            call_ids.append(call["id"])
        if msg.type == "tool":
            answered.append(msg.tool_call_id)

    assert len(call_ids) == 3, "1 次 task + 1 算距离 + 1 天气（搜索在子 agent 内部，不进主上下文）"
    assert sorted(call_ids) == sorted(answered), "每一条 tool_call 都要被回应"


def test_poi_snapshot_survives_into_final_state():
    """池子快照要能一路带到终态 —— M5 接 checkpointer 后它是"恢复后还能校验"的唯一依据。"""
    nodes = _happy_nodes()
    state = run(_run("去成都", nodes=nodes))

    assert len(state["collected_pois"]) >= 5
    assert all("lng" in v and "lat" in v for v in state["collected_pois"].values())


def test_render_leaves_no_error_on_clean_run():
    """`render` 的序列化往返检查在正常路径上必须安静通过。"""
    nodes = _happy_nodes()
    state = run(_run("去成都", nodes=nodes))
    assert not state.get("error")
    assert state["validation"]["remaining"] == []


# ══════════════════════════════════════════════════════════════
#  steering
# ══════════════════════════════════════════════════════════════


def test_pending_message_reaches_the_model_and_is_cleared():
    """🔴 M1 的验收判据之一：手动塞一条插话，验证下一圈被吃进去。

    "吃进去"要同时满足两件事：
    1. 它出现在**发给模型的消息**里（否则等于没收）
    2. `pending_messages` 被清空（否则每圈都重复插一遍，无限叠加）
    """
    nodes = _happy_nodes()
    state = run(_run("去成都", nodes=nodes, pending=["预算别超过人均800"]))

    sent = [str(m.content) for m in nodes.llm_tool.calls[0]]
    assert any("预算别超过人均800" in text for text in sent), "插话必须真的进了发给模型的消息"
    assert state["pending_messages"] == [], "吃完要清空"


def test_pending_message_does_not_break_normal_flow():
    nodes = _happy_nodes()
    state = run(_run("去成都", nodes=nodes, pending=["加个火锅店"]))

    assert state.get("trip")
    assert state["pending_messages"] == []
    assert not state.get("error")


# ══════════════════════════════════════════════════════════════
#  L3 打回
# ══════════════════════════════════════════════════════════════


def _repair_nodes(plan_script):
    return _nodes(
        extract_script=_intent(),
        tool_script=[_search_all(), ai_text("信息够了")],
        plan_script=plan_script,
    )


def test_one_repair_round_then_success():
    """第一版编了一个 id → 打回 → 第二版改对 → 出稿。`check_rounds` 必须恰好是 1。"""
    nodes = _repair_nodes(
        [ai_text(_draft(FAKE_ID)), ai_text(_draft(POI_IDS[0], POI_IDS[1]))]
    )
    state = run(_run("去成都", nodes=nodes))

    assert state["check_rounds"] == 1
    assert state["agent_rounds"] == 3, (
        "打回后要回 L2 再走一圈（agent1 → tool → agent2 → [repair] → agent3）"
    )
    assert state["trip"]["summary"]["hard_errors"] == 0
    assert state["validation"]["remaining"] == []


def test_repair_hint_reaches_generate_plan():
    nodes = _repair_nodes(
        [ai_text(_draft(FAKE_ID)), ai_text(_draft(POI_IDS[0], POI_IDS[1]))]
    )
    run(_run("去成都", nodes=nodes))

    prompts = nodes.llm_plan.joined_prompts()
    assert FAKE_ID in prompts, "第二版生成时必须看到上一次哪里错了"


def test_gives_up_after_two_rounds_and_outputs_degraded_trip():
    """🔴 打满 2 轮就**降级输出**，不是无限重排，也不是报错。

    用户要的是行程，不是预算报告。剩余风险写进 `validation.remaining`，
    由前端标红 —— 这比"什么都没有"有用得多。
    """
    nodes = _repair_nodes([ai_text(_draft(FAKE_ID))])  # 脚本用完会一直重复假 id
    state = run(_run("去成都", nodes=nodes))

    assert state["check_rounds"] == 2, "上限是 2 轮"
    assert state["trip"] is not None, "降级也要出东西"
    remaining = state["validation"]["remaining"]
    assert remaining, "剩余风险必须如实列出来"
    assert all(item["level"] == "hard" for item in remaining)
    assert state["trip"]["summary"]["hard_errors"] == len(remaining)
    # 模型一共被问了 3 次（初次 + 2 次重排），没有第 4 次
    assert nodes.llm_plan.call_count == 3


def test_degraded_trip_keeps_only_valid_stops():
    """降级输出的行程里，编造的站点必须**已经不在** —— 不能把假 id 留在产物里。"""
    nodes = _repair_nodes([ai_text(_draft(FAKE_ID))])
    state = run(_run("去成都", nodes=nodes))

    all_ids = [stop["poi_id"] for day in state["trip"]["days"] for stop in day["stops"]]
    assert FAKE_ID not in all_ids


# ── M3：`validate.py` 产生的硬错也要能驱动打回 ────────────────
#
# 上面三条用的是"编造的 poi_id" —— 那是 `assemble_trip` 阶段拦下的（`assemble_blocking`）。
# 但 M3 新加的 5 条判据走的是**另一条通道**（`report.blocking`）。
# 两条通道最后都拼进 `blocking` 交给条件边，**但没人验证过第二条真的通**。
#
# 这一条就是那个验证：让距离永远超限，于是每一轮都被 `reachable` 判成硬错，
# 打满 2 轮后降级输出。它同时钉住三件事：
#   ① `report.blocking` 能进打回理由（不是只写进 trip 就完了）
#   ② 打回上限对**所有**硬错一视同仁，不分类别
#   ③ 数据层面的问题（距离就是那么远）修不好时，循环**会停**，不会死转


class _FarProvider(MockAmapProvider):
    """距离永远 200 分钟的假 provider。

    ⚠️ **不改 `MockAmapProvider.calc_distance`**：那个实现是 2026-09-15
       实测校准过的（三段路网系数）。为了造失败场景去改它，
       等于让"校准"这件事失效 —— 以后真数据变了，没人分得清
       是模型偏差还是有人在测试里动过手。需要极端值时在**测试里**覆盖。
    """

    async def calc_distance(self, origin, dest):  # type: ignore[override]
        return DistanceResult(km=200.0, drive_min=200, straight_km=150.0)


def test_validation_hard_error_drives_the_repair_loop():
    """🔴 `validate.py` 的硬错必须**真的能打回**，且打满上限就收手。

    构造：一个距离永远 200 分钟的 provider。草稿里的 poi_id 全是真 id
    （所以 `assemble_trip` 一条都拦不住），但它排的每一跳车程都超限 ——
    **所有硬错都来自 M3 的判据**。

    期望：`check_rounds` 停在 2（不是 0，也不是无限），
    并且降级输出里如实列出剩余风险。
    """
    nodes = _nodes(
        provider=_FarProvider(today=TODAY),
        extract_script=_intent(),
        tool_script=[_search_all(), ai_text("信息够了")],
        # 一直给同一份"id 全真但路线很赶"的草稿 —— 脚本用完会重复最后一条
        plan_script=[ai_text(_draft(*POI_IDS[:3], days=2))],
    )
    state = run(_run("去成都", nodes=nodes))

    assert state["check_rounds"] == 2, "M3 的硬错必须能触发打回，且打到上限就停"
    remaining = state["validation"]["remaining"]
    assert remaining, "修不好就如实列出来"
    assert all("reachable" in item["code"] for item in remaining), (
        f"剩余风险该全是车程类：{remaining}"
    )
    assert state["trip"] is not None, "降级也要给用户一份行程"
    assert state["trip"]["summary"]["hard_errors"] == len(remaining)
    assert nodes.llm_plan.call_count == 3, "初次 + 2 次重排，没有第 4 次"


def test_validation_hard_error_message_reaches_the_model():
    """打回理由要**真的进 prompt** —— 否则模型重排时不知道该改什么，纯浪费钱。"""
    nodes = _nodes(
        provider=_FarProvider(today=TODAY),
        extract_script=_intent(),
        tool_script=[_search_all(), ai_text("信息够了")],
        plan_script=[ai_text(_draft(*POI_IDS[:3], days=2))],
    )
    run(_run("去成都", nodes=nodes))

    prompt = nodes.llm_plan.joined_prompts()
    assert "分钟" in prompt, "打回理由里必须带上具体耗时数字"
    assert "超过" in prompt, "要说清是超了阈值，不然模型只能瞎猜"


# ══════════════════════════════════════════════════════════════
#  D25 预算刹车
# ══════════════════════════════════════════════════════════════


def test_tool_call_budget_forces_generation():
    """工具调用达上限 → 不再问模型"你还想调吗"，直接用现有事实出稿。"""
    nodes = _nodes(
        extract_script=_intent(),
        tool_script=[
            _search_all(),
            ai_text("我还想再搜几次"),
        ],
        plan_script=[ai_text(_draft(POI_IDS[0], POI_IDS[1]))],
        max_tool_calls=1,
    )
    state = run(_run("去成都", nodes=nodes))

    assert state["tool_call_count"] == 1
    assert nodes.llm_tool.call_count == 1, "达上限后不该再问模型"
    assert state.get("trip"), "刹车后要照常出稿"


def test_agent_round_budget_forces_generation():
    """L2 轮数达上限（每轮只调一个工具的"反复空转"）→ 同样刹车。"""
    nodes = _nodes(
        extract_script=_intent(),
        tool_script=[
            _search_all(),
            ai_tool_call("search_poi", {"keyword": "公园", "city": "成都"}),  # 一直还想再搜
        ],
        plan_script=[ai_text(_draft(POI_IDS[0], POI_IDS[1]))],
        max_agent_rounds=2,
    )
    state = run(_run("去成都", nodes=nodes))

    assert state["agent_rounds"] == 2
    assert nodes.llm_tool.call_count == 2
    assert state.get("trip")


def test_forced_stop_message_is_visible():
    """刹车时留一条可见的消息 —— 免得用户以为"少排了几站是 AI 偷懒"。"""
    nodes = _nodes(
        extract_script=_intent(),
        tool_script=[_search_all()],
        plan_script=[ai_text(_draft(POI_IDS[0], POI_IDS[1]))],
        max_agent_rounds=1,
    )
    state = run(_run("去成都", nodes=nodes))

    contents = [str(m.content) for m in state["messages"]]
    assert any(FORCE_STOP_TEXT in c for c in contents)


# ══════════════════════════════════════════════════════════════
#  封闭世界的最后一道闸
# ══════════════════════════════════════════════════════════════


def test_model_that_never_searches_cannot_produce_a_trip():
    """🔴 模型一次工具都没调 → 池子空 → **拒绝生成**，并且**不花生成那次钱**。

    如果这里放行，模型会从记忆里编一份看起来完全合理的行程 ——
    而这正是整个项目要证明"它不会发生"的那件事。
    """
    nodes = _nodes(
        extract_script=_intent(),
        tool_script=[ai_text("我直接给你排好了：第一天去武侯祠……")],
        plan_script=[ai_text(_draft())],
    )
    state = run(_run("去成都", nodes=nodes))

    assert not state.get("trip"), "池子空时不该有行程产物"
    assert "候选池是空的" in (state.get("error") or "")
    assert nodes.llm_plan.call_count == 0, "既然注定要拦，就别花这次钱"
    assert state["agent_rounds"] == 1, "模型这边只被问过一次"


def test_search_that_finds_nothing_also_blocks_generation():
    """搜了但**一个都没搜到**（比如"火锅"——mock 池里没有餐饮）时同样拦住。

    这条区分了两种情况：模型"没搜"和"搜了但空"。后者同样不能生成 ——
    池子空就是池子空，没有候选就没法在封闭世界里排行程。
    """
    nodes = _nodes(
        extract_script=_intent(),
        tool_script=[ai_tool_call("search_poi", {"keyword": "火锅", "city": "成都"}), ai_text("没搜到")],
        plan_script=[ai_text(_draft())],
    )
    state = run(_run("去成都吃火锅", nodes=nodes))

    assert not state.get("trip")
    assert "候选池是空的" in (state.get("error") or "")
    # ⚠️ 这一轮**花掉了一次**工具调用 —— 那是应该花的，因为"搜不到"是必须知道的事实
    assert state["tool_call_count"] == 1


def test_graph_still_terminates_when_everything_fails():
    """全链路失败也必须**走到终点**，不能挂住或抛图外异常。

    理由：节点里抛异常会让已经流出的 SSE token 变成孤儿
    （前端看到半句话然后断流）。所以失败要能走完 `render`。
    """
    nodes = _nodes(
        extract_script=_intent(),
        tool_script=[ai_text("不搜了")],
        plan_script=[ai_text("不是 JSON")],
    )
    state = run(_run("去成都", nodes=nodes))

    assert state.get("error")
    assert not state.get("trip")
    assert len(state["messages"]) > 1, "`render` 跑过了（图走到了终点）"


def test_weather_outside_window_does_not_kill_the_graph():
    """天气查不到（行程在 4 天窗口外）不该让整条链失败 —— 标 unavailable 就行。"""
    nodes = _nodes(
        extract_script=_intent(script_date="2026-12-01"),
        tool_script=[_search_all(), ai_text("够了")],
        plan_script=[
            ai_text(
                json.dumps(
                    {
                        "title": "冬季行程",
                        "days": [
                            {
                                "date": "2026-12-01",
                                "theme": "x",
                                "stops": [
                                    {
                                        "poi_id": POI_IDS[0],
                                        "arrive": "09:00",
                                        "stay_min": 90,
                                        "match_reason": "r",
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        ],
    )
    state = run(_run("12月1日去成都", nodes=nodes))

    assert state.get("trip")
    weather = state["trip"]["days"][0]["weather"]
    assert weather["status"] == "unavailable"
    assert weather["note"], "拿不到就要说清为什么，不能只留个空"


def test_total_tool_executions_never_exceed_budget():
    """🔴 端到端复现那个实测缺口：模型每轮发 5 个调用、上限设 3。

    `tool_call_count` 记的是**模型请求了几次**（会超），
    真正该封顶的是**执行了几次**。这条断言盯的是后者。
    """
    nodes = _nodes(
        extract_script=_intent(),
        tool_script=[_search_all()],  # 永远一次想搜 5 个（现在 = 一次 task，内部自己试错）
        plan_script=[ai_text(_draft(POI_IDS[0], POI_IDS[1]))],
        max_tool_calls=3,
    )
    state = run(_run("去成都", nodes=nodes))

    executed = [
        m
        for m in state["messages"]
        if m.type == "tool" and TOOL_BUDGET_TEXT not in str(m.content)
    ]
    assert len(executed) <= 3, f"实际执行了 {len(executed)} 次，超过上限 3"

    # 但每一条请求都必须有回应（否则真实模型下会 400）
    requested = [
        call["id"] for m in state["messages"] for call in (getattr(m, "tool_calls", None) or [])
    ]
    answered = [m.tool_call_id for m in state["messages"] if m.type == "tool"]
    assert sorted(requested) == sorted(answered)


def test_ask_more_is_a_terminal_state():
    """`ask_more` 后面不该再有节点跑 —— 它是终点之一，不是中转站。"""
    graph = build_graph(_nodes())
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    assert ("ask_more", "__end__") in edges
    assert ("render", "__end__") in edges


# ══════════════════════════════════════════════════════════════
#  软判据（M3 欠账 · D45）
#
#  这一节测的是**接线**不是判据本身（判据在 test_soft.py）：
#  ① 软校验是不是真的挂在定稿路径上  ② 它挂了会不会把图带下水
# ══════════════════════════════════════════════════════════════


def test_soft_check_runs_on_the_final_trip():
    """跑完整图 → 软判据出现在**最终那一版** `Trip` 的三层 `checks` 里。

    ⚠️ 为什么必须是"最终那一版"：如果把 `soft_check` 挂在 `check_plan` 里，
    打回重排之后第一次跑出来的软判据会**留在一份已经作废的行程上**，
    而重排后的新版可能一条都没有 —— 用户看到的提醒和行程对不上。
    """
    nodes = _nodes(
        extract_script=_intent(),
        tool_script=[_search_all(), ai_text()],
        plan_script=[ai_text(_draft(POI_IDS[0], POI_IDS[1]))],
        soft_script=[
            soft_says(
                {
                    "code": "needs_booking",
                    "status": "fail",
                    "msg": "这里节假日建议提前确认预约政策",
                    "day": 1,
                    "seq": 1,
                },
                {"code": "overall_feasible", "status": "pass"},
            )
        ],
    )
    state = run(_run("去成都", nodes=nodes))
    trip = state["trip"]

    assert trip["summary"]["soft_warnings"] == 1
    # ⚠️ 站上同时挂着硬判据（poi_exists / open_today）—— **只挑软判据看**。
    # 直接断言整个列表会顺带把硬判据也钉住，将来加一条硬判据就红一片。
    soft_codes = [c["code"] for c in trip["days"][0]["stops"][0]["checks"] if c["level"] == "soft"]
    assert soft_codes == ["needs_booking"]
    assert [c["code"] for c in trip["checks"]] == ["overall_feasible"]
    assert state["soft_report"]["applied"] == 2


def test_soft_fail_never_creates_blocking_or_repair():
    """🔴 软判据 `fail` **不打回** —— 这是"硬软分层"的全部意义。

    如果它进了 `blocking`，agent 会去打回重排一个**它永远修不好**的东西
    （软判据说的是"建议确认预约政策"，不是行程排错了），
    白烧 2 轮 LLM 之后还是原样输出。
    """
    nodes = _nodes(
        extract_script=_intent(),
        tool_script=[_search_all(), ai_text()],
        plan_script=[ai_text(_draft(POI_IDS[0], POI_IDS[1]))],
        soft_script=[
            soft_says(
                {
                    "code": "elder_friendly",
                    "status": "fail",
                    "msg": "这一天步行量对长辈偏紧，可以考虑减少一站",
                    "day": 1,
                }
            )
        ],
    )
    state = run(_run("去成都", nodes=nodes))

    assert state["trip"]["validation"]["rounds"] == 0, "软判据不该触发打回"
    assert state["trip"]["summary"]["hard_errors"] == 0
    assert state["trip"]["summary"]["soft_warnings"] == 1


def test_soft_llm_crash_still_produces_a_trip():
    """🔴 **软判据挂了，行程必须照样出来。**

    这是 `soft_check` 与 `generate_plan` 纪律**相反**的地方：
    生成节点失败必须写 `state["error"]`（用户不能什么都拿不到），
    而软判据失败**绝不能**写 —— 那会为了几句可有可无的提醒
    废掉一份本来完全可用的行程。代价完全不对等。

    用 `BoomChatModel` 而不是"返回脏文本"：真实故障就是超时/503/连接断开，
    而"返回脏文本"只覆盖了"模型不听话"那一种，测不到异常路径。
    """
    nodes = _nodes(
        extract_script=_intent(),
        tool_script=[_search_all(), ai_text()],
        plan_script=[ai_text(_draft(POI_IDS[0], POI_IDS[1]))],
        soft_script=[],  # 会被下面整个换掉
    )
    nodes.llm_soft = BoomChatModel()
    state = run(_run("去成都", nodes=nodes))

    assert not state.get("error"), f"软判据挂了不该让整次请求变成错误：{state.get('error')}"
    assert state["trip"]["days"], "行程必须仍然产出来"
    assert state["trip"]["summary"]["soft_warnings"] == 0
    assert state["soft_report"]["error"], "失败原因要留在账本里，不能静默吞掉"
    assert all(c["level"] != "soft" for c in state["trip"]["checks"])
