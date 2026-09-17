"""搜索子 agent（M4，D5）—— 隔离、封闭世界、降级、trace 四条线各自盯死。

这个文件盯的是**子 agent 自己的契约**；它和主图的接线（池子共享、
task 计 1 次调用）在 `test_nodes.py` / `test_graph.py` 里。

═══════════════════════════════════════════════════════════════
 四条最值钱的断言
═══════════════════════════════════════════════════════════════

**① 上下文隔离是真实的。**
   子 agent 的中间过程（搜索往来、关键词试错）**不进主 messages** ——
   这是 D5"收窄上下文"的全部意义。如果哪天有人把子图消息接进主图，
   主上下文会悄悄膨胀，唯一能抓住它的就是这里的断言。

**② 终选的 poi_id 逐个过池子。**
   子 agent 的模型也是模型，也会编。它在出口处被拦一道，
   主图的 `assemble_blocking` 就永远不需要为 task 的产物买单。

**③ 超限降级不抛异常。**
   规划 LLM 永远不说 final / 搜索全失败 —— 子 agent 必须**自己收尾**，
   把"未精选的可用候选"端回去。抛异常 = 主图的 ToolMessage 变成报错文案
   = 主 agent 大概率重试整个 task，白烧一轮。

**④ trace 是结构化的、可断言的。**
   M4 验收"trace 可见"不是"打了 log"，是 state 里有结构化记录 ——
   M6 过程可视化（前端画"子 agent 在搜什么"）的数据来源就是它。
"""

from __future__ import annotations

from datetime import date

from app.graph.subagent import (
    MAX_SUB_ROUNDS,
    MAX_SUB_SEARCHES,
    build_search_subagent,
    build_task_tool,
    current_searched_keywords,
    drain_subagent_trace,
    keyword_ledger_scope,
    note_searched_keywords,
    subagent_trace_scope,
)
from app.providers.mock import MOCK_CITY, MOCK_POI_POOL, MockAmapProvider
from app.schemas import AmapPoi
from app.tools.amap_tools import build_amap_tools
from app.tools.poi_pool import PoiPool, poi_pool_scope
from fakes import (
    POI_IDS,
    ScriptedChatModel,
    ai_text,
    ai_tool_call,
    make_nodes,
    make_state,
    run,
    sub_finds,
)

TODAY = date(2026, 9, 16)
FAKE_ID = "B0000_THIS_IS_MADE_UP"


def _search_tool(provider: MockAmapProvider | None = None):
    tools = build_amap_tools(provider or MockAmapProvider(today=TODAY), default_city=MOCK_CITY)
    return {t.name: t for t in tools}["search_poi"]


def _run_sub(script, *, pool: PoiPool | None = None, search_tool=None) -> dict:
    """跑一次子图（在池子作用域内），返回终态。"""
    subgraph = build_search_subagent(
        llm=ScriptedChatModel(script=script), search_tool=search_tool or _search_tool()
    )
    initial = {
        "objective": "成都适合带老人慢走的景点",
        "city": "成都",
        "notes": [],
        "keywords": [],
        "new_keywords": [],
        "search_calls": 0,
        "rounds": 0,
    }
    if pool is None:
        pool = PoiPool()
    with poi_pool_scope(pool):
        return run(subgraph.ainvoke(initial))


# ══════════════════════════════════════════════════════════════
#  正常路径
# ══════════════════════════════════════════════════════════════


def test_normal_path_search_then_final():
    """搜一轮 → 终选：回流文本里有选中的 POI 与理由，trace 记录全过程。

    ⚠️ drain 必须写在 scope **里面** —— scope 退出后 `_trace_var` 已复位，
    drain 拿到的是 None。这条注释是给下一个写子 agent 测试的人的：
    "取记录要在作用域内取"，不然你会以为 trace 丢了。
    """
    with subagent_trace_scope():
        result = _run_sub(
            sub_finds("武侯祠", picks=[{"poi_id": POI_IDS[0], "reason": "步道平缓"}])
        )
        records = drain_subagent_trace()

    assert "武侯祠" in result["final_text"], "终选清单要带上 POI 名"
    assert "步道平缓" in result["final_text"], "模型给的理由要回流 —— 主 agent 靠它判断合不合适"
    assert "（共 1 条" in result["final_text"]

    assert len(records) == 1
    record = records[0]
    assert record["objective"] == "成都适合带老人慢走的景点"
    assert record["keywords"] == ["武侯祠"]
    assert record["searches"] == 1
    assert record["returned"] == 1
    assert record["degraded"] is False
    assert record["llm_failed"] is False


def test_sub_llm_sees_only_the_objective_not_the_main_conversation():
    """🔴 上下文隔离的**正向**断言：子 agent 只看到 objective。

    隔离有两个方向：子 → 主（中间过程不回流，下一条测），
    主 → 子（主对话不泄漏）。后者保证了 objective 参数的纪律是有意义的 ——
    如果子 agent 能看到主对话，"objective 必须自包含"就是假要求。
    """
    script = sub_finds("武侯祠", picks=[{"poi_id": POI_IDS[0], "reason": "r"}])
    sub_llm = ScriptedChatModel(script=script)
    subgraph = build_search_subagent(llm=sub_llm, search_tool=_search_tool())
    with poi_pool_scope(PoiPool()):
        run(
            subgraph.ainvoke(
                {
                    "objective": "成都的历史古迹",
                    "city": "成都",
                    "notes": [],
                    "keywords": [],
                    "search_calls": 0,
                    "rounds": 0,
                }
            )
        )

    prompts = sub_llm.joined_prompts()
    assert "成都的历史古迹" in prompts
    # 主对话里的典型内容不该出现在子 agent 的任何一轮 prompt 里
    assert "下周三" not in prompts
    assert "用户" not in prompts or "用户" in "主规划师交给你", "只允许系统提示里出现'用户'字样"


# ══════════════════════════════════════════════════════════════
#  封闭世界：终选过池子
# ══════════════════════════════════════════════════════════════


def test_invented_poi_id_is_dropped_at_the_exit():
    """🔴 子 agent 模型编的 id 在出口被丢弃 —— 封闭世界的最后一道闸。

    构造：final picks 里一半真一半假。假 id 不在池子里（没被搜出来过），
    绝不能出现在回流文本里 —— 否则主 agent 会把它当真候选排进行程，
    到 `assemble_trip` 才被拦下，白打回一轮。
    """
    result = _run_sub(
        sub_finds(
            "武侯祠",
            picks=[
                {"poi_id": FAKE_ID, "reason": "编的"},
                {"poi_id": POI_IDS[0], "reason": "真的"},
            ],
        )
    )

    assert FAKE_ID not in result["final_text"]
    assert POI_IDS[0] in result["final_text"]
    with subagent_trace_scope():
        _run_sub(
            sub_finds(
                "武侯祠",
                picks=[{"poi_id": FAKE_ID, "reason": "编的"}, {"poi_id": POI_IDS[0], "reason": "真的"}],
            )
        )
        assert drain_subagent_trace()[0]["returned"] == 1, "trace 里的 returned 是过滤后的数"


def test_final_with_no_results_says_so_instead_of_fabricating():
    """搜过但什么都没搜到 + 模型给空 picks → 如实说"没有找到"，**不端降级清单**。

    模型明确说"没有合适的"是一个**结论**，必须回流给主 agent ——
    主 agent 看到"没找到"才会决定少排一站或换任务描述。
    用池子里的存货覆盖它，等于把"这个方向没有结果"这个事实藏起来。
    """
    result = _run_sub(sub_finds("火锅", "成都 餐厅"))  # mock 池里没有餐饮

    assert "没有找到合适的候选" in result["final_text"]
    assert "不要凭记忆填任何地点" in result["final_text"], "回流文本要把纪律一起带上"


# ══════════════════════════════════════════════════════════════
#  降级：超限 / 失败都不许抛
# ══════════════════════════════════════════════════════════════


def test_budget_exhausted_forces_degraded_final_from_pool():
    """🔴 规划 LLM 永远说 search → 打满 MAX_SUB_ROUNDS 强制收尾。

    收尾**不抛异常**：把池里已拿到的站点型候选按重排顺序端出去，
    文本明确标注"未经精选"。这是 D25"超限降级输出"在子 agent 里的同款纪律。
    """
    script = [
        ai_text('{"action": "search", "keywords": ["景点"]}'),
        ai_text('{"action": "search", "keywords": ["古迹"]}'),
        ai_text('{"action": "search", "keywords": ["公园"]}'),
        # 脚本用完会重复最后一条 —— 但轮数上限先到，循环必须停
    ]
    result = _run_sub(script)

    assert "未经精选" in result["final_text"], "降级清单必须亮明身份"
    assert "武侯祠" in result["final_text"], "池里搜到过的候选要端出来"

    with subagent_trace_scope():
        _run_sub(script)
        record = drain_subagent_trace()[0]
    assert record["degraded"] is True
    assert record["rounds"] == MAX_SUB_ROUNDS, "轮数恰好停在上限，不是无限"
    assert record["returned"] > 0


def test_repeated_keyword_does_not_burn_the_search_budget():
    """规划 LLM 重复给同一个关键词 → 去重后**不再执行搜索**。

    不去重的话，"模型卡在一个词上"会烧光 MAX_SUB_SEARCHES 次搜索预算，
    而且每次都是同样的结果 —— 纯浪费 provider 配额（高德有 QPS 限流，坑 12）。
    """
    script = [ai_text('{"action": "search", "keywords": ["景点"]}')]  # 永远同一个词
    with subagent_trace_scope():
        _run_sub(script)
        record = drain_subagent_trace()[0]

    assert record["searches"] == 1, "同一个词只搜一次"
    assert record["degraded"] is True, "关键词不再有新信息 → 轮数耗尽 → 降级收尾"


def test_search_failure_does_not_kill_the_subagent():
    """🔴 搜索全失败 → 子 agent 自己收尾说"没找到"，**绝不抛异常**。

    抛出去 = 主图的 ToolMessage 变成报错文案 = 主 agent 重试整个 task
    = 同样的失败再来一遍。失败信息放进 notes 让规划 LLM 看到、
    让它换词 —— 换不了就由轮数上限兜底。
    """

    class ExplodingSearch:
        """search_poi 一调就炸的搜索工具（天气/距离不涉及，不实现）。"""

        name = "search_poi"

        async def ainvoke(self, args):
            raise RuntimeError("高德 503")

    result = _run_sub(sub_finds("武侯祠", picks=[]), search_tool=ExplodingSearch())

    assert "没有找到" in result["final_text"], "如实说没找到，不装作成功"

    with subagent_trace_scope():
        _run_sub(sub_finds("武侯祠", picks=[]), search_tool=ExplodingSearch())
        record = drain_subagent_trace()[0]
    assert record["returned"] == 0
    assert record["degraded"] is False, "模型明确说没找到 ≠ 降级，两者语义不同"


def test_fallback_candidates_exclude_hotels_and_non_visit_types():
    """降级清单**只端能当站点的**：住宿（D1 不排住宿）与交通设施类被滤掉。

    降级清单是"预算用尽"的兜底 —— 如果它把酒店端进候选，
    生成节点可能真的把酒店排成一站"景点"，那是一个产品级的事故。
    """
    from app.graph.subagent import _fallback_candidates
    from app.tools.poi_rank import rank_pois

    pool = PoiPool()
    dumps = []
    for poi in MOCK_POI_POOL[:3]:
        dumps.append(AmapPoi.model_validate(poi.model_dump(mode="json")))
    hotel = AmapPoi.model_validate(
        {**MOCK_POI_POOL[0].model_dump(mode="json"), "poi_id": "HOTEL_1", "type": "住宿服务;星级宾馆"}
    )
    road = AmapPoi.model_validate(
        {**MOCK_POI_POOL[0].model_dump(mode="json"), "poi_id": "ROAD_1", "type": "交通设施服务;地铁站"}
    )
    pool.record([*dumps, hotel, road])

    picks = _fallback_candidates(pool)
    picked_ids = [p["poi_id"] for p in picks]

    assert "HOTEL_1" not in picked_ids, "住宿不能进降级清单（D1：行程不排住宿）"
    assert "ROAD_1" not in picked_ids, "交通设施不能进降级清单（坑 25 的同类）"
    assert picked_ids, "正常景点要留在清单里"
    assert picked_ids == [
        p.poi_id for p in rank_pois([*dumps, hotel, road]) if p.poi_id in picked_ids
    ], "顺序要按重排档位来"


# ══════════════════════════════════════════════════════════════
#  trace 通道
# ══════════════════════════════════════════════════════════════


def test_trace_outside_scope_is_discarded_not_crashing():
    """没有 trace 作用域时子图必须照常跑 —— 只是记录无处可去。

    这条守的是"trace 是锦上添花"的定位：万一哪天调用方忘了开 scope
    （或 M6 的某个新入口漏了），子 agent 不能因此挂掉。
    """
    result = _run_sub(sub_finds("武侯祠", picks=[{"poi_id": POI_IDS[0], "reason": "r"}]))
    assert "武侯祠" in result["final_text"]
    assert drain_subagent_trace() == []

    # 🔴 2026-09-16 实测抓到的泄漏：旧版 drain 在作用域外会顺手 `set([])`，
    # 在环境上下文里留一个没人管理的列表 —— 后续所有子图的记录都往里堆、
    # 永远没人取（全套件跑时表现为"drain 返回了前几个测试的 6 条旧记录"）。
    from app.graph.subagent import _trace_var

    assert _trace_var.get() is None, "作用域外 drain 不能在上下文里留下泄漏的列表"


def test_trace_drain_clears_what_it_returns():
    """drain 取完即清 —— 不清的话，第二次 tool_step 会把上一次的记录再交一遍。"""
    with subagent_trace_scope():
        _run_sub(sub_finds("武侯祠", picks=[{"poi_id": POI_IDS[0], "reason": "r"}]))
        first = drain_subagent_trace()
        second = drain_subagent_trace()

    assert len(first) == 1
    assert second == [], "取完必须清空"


# ══════════════════════════════════════════════════════════════
#  task 工具本身
# ══════════════════════════════════════════════════════════════


def test_task_tool_rejects_empty_objective():
    subgraph = build_search_subagent(
        llm=ScriptedChatModel(script=[]), search_tool=_search_tool()
    )
    task = build_task_tool(subgraph=subgraph, default_city=MOCK_CITY)

    out = run(task.ainvoke({"objective": "   "}))
    assert "没法搜索" in out, "空任务要给指引，不是空字符串也不是报错"


def test_task_tool_defaults_city_to_destination():
    """city 留空 → 用本次行程的目的地兜底。子 agent 的 prompt 里城市不能是"未指定"。"""
    sub_llm = ScriptedChatModel(
        script=sub_finds("武侯祠", picks=[{"poi_id": POI_IDS[0], "reason": "r"}])
    )
    subgraph = build_search_subagent(llm=sub_llm, search_tool=_search_tool())
    task = build_task_tool(subgraph=subgraph, default_city="成都")

    with poi_pool_scope(PoiPool()):
        run(task.ainvoke({"objective": "历史古迹"}))

    assert "成都" in sub_llm.joined_prompts(), "默认城市要进子 agent 的任务描述"


# ══════════════════════════════════════════════════════════════
#  D71：已搜关键词台账（跨 task 去重里"过程"那一半）
# ══════════════════════════════════════════════════════════════


def test_keyword_ledger_collects_executed_keywords():
    """子 agent 真正搜过的词要落进台账 —— 它是"同一个词别搜第二遍"的唯一依据。

    D65 只传"结果"（池子里有哪些地点），拦不住"换个说法问同一批地点"：
    实测成都在一轮里 task1/task3/task4 三次覆盖「杜甫草堂 / 武侯祠 / 锦里」。
    """
    ledger: list[str] = []
    with poi_pool_scope(PoiPool()), keyword_ledger_scope(ledger):
        _run_sub(sub_finds("武侯祠", "锦里", picks=[]))
    assert ledger == ["武侯祠", "锦里"]


def test_keyword_ledger_only_records_executed_keywords():
    """🔴 预算撞顶时**没跑的那个词不许记**：把"没搜过"记成"搜过了"是**不可逆**的
    信息错误 —— 下一个 task 会被提示"别用这个词"，而它其实从来没被搜过。

    （反过来"搜过了却没记"只是少省一点，无副作用 —— 所以这里宁可漏记不误记。）
    """
    ledger: list[str] = []
    subgraph = build_search_subagent(
        llm=ScriptedChatModel(script=sub_finds("武侯祠", "锦里", picks=[])),
        search_tool=_search_tool(),
    )
    initial = {
        "objective": "成都适合带老人慢走的景点",
        "city": "成都",
        "notes": [],
        "keywords": [],
        "new_keywords": [],
        "search_calls": MAX_SUB_SEARCHES - 1,  # 只够跑一个词
        "rounds": 0,
    }
    with poi_pool_scope(PoiPool()), keyword_ledger_scope(ledger):
        run(subgraph.ainvoke(initial))

    assert ledger == ["武侯祠"], "第二个词没执行，不许进台账"


def test_keyword_ledger_dedupes_and_survives_multiple_tasks():
    """台账是**累计 + 去重**的：第二个 task 里重复的词不会再记一遍。"""
    ledger: list[str] = []
    with poi_pool_scope(PoiPool()), keyword_ledger_scope(ledger):
        _run_sub(sub_finds("武侯祠", picks=[]))
        _run_sub(sub_finds("武侯祠", "锦里", picks=[]))
    assert ledger == ["武侯祠", "锦里"]


def test_keyword_ledger_outside_scope_is_noop():
    """作用域外调用是**无操作**（与 trace 同一纪律：没有作用域 ≠ 作用域是空的）。"""
    note_searched_keywords(["随便一个词"])
    assert current_searched_keywords() == []


def test_task_tool_puts_searched_keywords_into_objective():
    """新 task 的描述里要带上"这些词搜过了" —— 这就是 D71 的落点。"""
    sub_llm = ScriptedChatModel(script=sub_finds("武侯祠", picks=[]))
    subgraph = build_search_subagent(llm=sub_llm, search_tool=_search_tool())
    task = build_task_tool(subgraph=subgraph, default_city="成都")

    with poi_pool_scope(PoiPool()), keyword_ledger_scope(["成都 博物馆", "成都 火锅"]):
        run(task.ainvoke({"objective": "成都适合带老人的餐厅"}))

    prompt = sub_llm.joined_prompts()
    assert "这些关键词前面已经搜过" in prompt
    assert "成都 博物馆" in prompt and "成都 火锅" in prompt


# ══════════════════════════════════════════════════════════════
#  与主图 tool_step 的接线：隔离 + trace 落 state
# ══════════════════════════════════════════════════════════════


def test_tool_step_returns_only_the_final_text_from_the_subagent():
    """🔴 隔离的**反向**断言：子 agent 的中间过程不进主 messages。

    tool_step 交给主上下文的**只有一条 ToolMessage**（内容 = 终选文本）。
    如果哪天有人把子图的 HumanMessage / notes 也接了出来，
    主上下文会随每次 task 膨胀几十条消息 —— D5 就白做了。
    """
    nodes = make_nodes(
        tool_script=[ai_tool_call("task", {"objective": "成都的历史古迹", "city": "成都"})],
        sub_script=sub_finds("武侯祠", picks=[{"poi_id": POI_IDS[0], "reason": "步道平缓"}]),
    )
    ai = ai_tool_call("task", {"objective": "成都的历史古迹", "city": "成都"})
    out = run(nodes.tool_step(make_state(messages=[ai])))

    assert len(out["messages"]) == 1, "主上下文里只多一条 ToolMessage"
    tool_msg = out["messages"][0]
    assert tool_msg.type == "tool"
    assert "武侯祠" in tool_msg.content
    assert "## 关键词" not in tool_msg.content, "子 agent 的搜索笔记格式不能泄漏进主上下文"


def test_tool_step_writes_structured_trace_into_state():
    """M4 验收的落点：task 调用后，state 的 `subagent_trace` 里有结构化记录。

    trace 走 `operator.add` reducer —— tool_step 只返回本次新增。
    这条断言钉住"tool_step 真的把 trace 交了出来"，防止哪天
    drain 和 update 之间断了线（trace 静默消失，前端的过程可视化跟着空白）。
    """
    nodes = make_nodes(
        tool_script=[ai_tool_call("task", {"objective": "成都的历史古迹", "city": "成都"})],
        sub_script=sub_finds("武侯祠", picks=[{"poi_id": POI_IDS[0], "reason": "r"}]),
    )
    ai = ai_tool_call("task", {"objective": "成都的历史古迹", "city": "成都"})
    out = run(nodes.tool_step(make_state(messages=[ai])))

    trace = out["subagent_trace"]
    assert len(trace) == 1
    record = trace[0]
    for key in ("objective", "city", "rounds", "searches", "keywords", "returned", "degraded", "llm_failed"):
        assert key in record, f"trace 记录缺字段 {key} —— M6 过程可视化靠它画图"
    assert record["city"] == "成都"
    assert record["returned"] == 1


def test_tool_call_count_counts_task_not_inner_searches():
    """🔴 主预算的会计口径：task 计 1 次，子 agent 内部搜索**不占**主图预算。

    这是结构决定的（工具函数写不进主 state），但要防的是反方向的回归：
    哪天有人把"子 agent 内部搜索次数"也累加进 `tool_call_count`，
    一次 task 就吃掉 6 次预算，主 agent 会被迫提前刹车。
    """
    nodes = make_nodes(
        tool_script=[ai_tool_call("task", {"objective": "成都的历史古迹", "city": "成都"})],
        sub_script=sub_finds("武侯祠", "锦里", picks=[{"poi_id": POI_IDS[0], "reason": "r"}]),
    )
    ai = ai_tool_call("task", {"objective": "成都的历史古迹", "city": "成都"})
    out = run(nodes.tool_step(make_state(messages=[ai])))

    assert out["tool_call_count"] == 1, "task 就是 1 条 tool_call"
    trace = out["subagent_trace"][0]
    assert trace["searches"] == 2, "子 agent 内部搜了 2 次（记进 trace，不记进主预算）"


def test_subagent_constants_leave_headroom_under_main_budget():
    """子 agent 的内部预算要在文档承诺的账内：3 次 task × (4 轮 + 6 搜索)。

    常量被人随手调大 = 最坏成本翻倍，而那笔账是写进 DECISIONS 的。
    用断言钉住常量，比"注释里写数字"可靠 —— 注释没人看，断言天天跑。
    """
    assert MAX_SUB_ROUNDS == 4
    assert MAX_SUB_SEARCHES == 6
