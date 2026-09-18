"""图的组装。**所有条件边都在这个文件里** —— 想知道"下一步去哪"，只看这里。

═══════════════════════════════════════════════════════════════
 拓扑
═══════════════════════════════════════════════════════════════

```
              ┌──────────────┐
   entry ───► │ parse_intent │  ⑧ 抽需求
              └──────┬───────┘
                     │ 缺 destination / date？
          ┌──────────┴───────────┐
          │是                     │否
          ▼                       ▼
    ┌───────────┐          ┌───────────────┐        ┌───────────┐
    │ ask_more  │  ⑨       │ make_skeleton │  ⑩     │  repair   │ ⑦
    └─────┬─────┘          └──────┬────────┘        └─────▲─────┘
          │                       │ 快速骨架（不调工具）    │
          │                ┌──────┴────────┐              │
          │                │  agent_step   │◄─────────────┤
          │                └──────┬────────┘              │
          │                       │ 模型还要调工具？       │
          │                ┌──────┴──────────┐            │
          │                │是                │否          │
          │                ▼                  ▼            │
          │          ┌───────────┐    ┌───────────────┐   │
          │          │ tool_step │ ②  │ generate_plan │   │
          │          └─────┬─────┘    └───────┬───────┘   │
          │                │ 回 L2            │ ③         │
          │                └──► agent_step    ▼            │
          │                              ┌────────────────┐│
          │                              │   check_plan   │┘
          │                              └────────┬───────┘
          │                                       │ 无硬错 / 已达打回上限
          │                                       ▼
          │                              ┌────────────────┐
          │                              │   soft_check   │ ⑥ 只提醒，不打回
          │                              └────────┬───────┘
          │                                       ▼
          │                                 ┌───────────┐
          │                                 │  render   │  ⑤
          │                                 └─────┬─────┘
          │                                      END
```

═══════════════════════════════════════════════════════════════
 为什么校验回环回到 `agent_step` 而不是 `generate_plan`
═══════════════════════════════════════════════════════════════

硬错是"某个 `poi_id` 不在候选池里"。要修它，只有两条路：

| 走法 | 结果 |
|---|---|
| 回 `generate_plan` | 池子没变，模型在**同一份清单**里再挑一次 —— 挑对的概率取决于它上次为什么挑错，**没有新信息注入** |
| ✅ 回 `agent_step` | 模型能**重新搜**（换关键词、把上次搜漏的地点补上），搜索工具会真的往池子里加新东西 —— **池子可能变大，这才有出路** |

所以回环的目标是 **L2**，不是生成节点。这也让"重排"和"重查"合成了一件事：
模型自己决定是再搜几个点，还是直接在现有池子里换一个 id。

═══════════════════════════════════════════════════════════════
 关于 `recursion_limit`
═══════════════════════════════════════════════════════════════

LangGraph 默认 25 步。本图的最坏路径：

```
parse_intent(1) + [agent_step + tool_step] × 8 + generate_plan(1)
  + check_plan(1) + repair(1) + [再走 8 轮] + generate_plan + check_plan + render
```

远不止 25。**必须显式放宽**，否则图会在半路抛
`GraphRecursionError` —— 而那看起来像"程序崩了"，不像"预算用完了"。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.config import settings
from app.graph.nodes import MAX_AGENT_ROUNDS, MAX_CHECK_ROUNDS, MAX_TOOL_CALLS, Nodes
from app.graph.state import TripState
from app.providers import build_provider
from app.providers.base import AmapProvider
from app.tools.amap_tools import build_amap_tools

RECURSION_LIMIT = 80
"""给图的总步数上限。**这不是业务上限** —— 业务上限是 D25 那三个
（L2≤8、L3≤2、工具调用≤20），它们各自在节点与条件边里守着，会**优雅降级**。
这里只是兜底：万一有哪个条件边写错了导致真的绕不出去，让 LangGraph 抛出来，
而不是无限跑下去把 API 余额烧完。"""


def build_runtime(
    *,
    provider: AmapProvider | None = None,
    today: Any = None,
    model: str | None = None,
) -> Nodes:
    """组装一次运行需要的全部依赖。

    ⚠️ **这是唯一一处把 provider / 工具 / 三个 LLM 凑到一起的地方。**

    `model` = 会话级模型覆盖（A45/D55）：两槽位都换成它，思考开关仍按节点类型；
    None = `.env` 默认。粘贴 / recheck 不传它（不挂会话，永远默认）。
    节点本身只依赖 `Nodes` 这个形状，所以测试里可以整个换掉
    （塞 mock provider、塞假 LLM），不需要动图结构。
    """
    from app.graph.subagent import build_search_subagent, build_task_tool
    from app.llm import (
        build_extract_llm,
        build_plan_llm,
        build_skel_llm,
        build_soft_llm,
        build_sub_llm,
        build_tool_llm,
    )
    from app.providers.mock import MOCK_CITY, MockAmapProvider

    if provider is not None:
        active_provider = provider
    elif settings.mock_mode and today is not None:
        # 把"今天"也传给 mock —— 否则会出现一种很难查的错位：
        # 抽取节点按 `today` 算相对日期，而天气窗口按**真实今天**算，
        # 于是"行程日期全在窗口内"和"天气全查不到"同时成立。
        # ⚠️ 只在 mock 模式下这么做：real provider 的 `today` 是服务端事实，
        # 传一个假的进去只会让它去查一个不存在的日期。
        active_provider = MockAmapProvider(today=today)
    else:
        active_provider = build_provider(settings)

    # 兜底城市。**只在模型没传 `city` 参数时生效** ——
    # 真实运行时模型一般会带上目的地，所以命中率很低；
    # 它换来的是"工具签名里 city 有默认值"，模型不用被迫每次都填。
    # mock 与 real 用同一个值是有意的：**换 provider 不该改变 prompt 里的默认值**，
    # 否则"切到真实数据源"这一个动作会顺带改变模型看到的世界。
    default_city = MOCK_CITY

    tools = build_amap_tools(active_provider, default_city=default_city)
    by_name = {t.name: t for t in tools}

    # ══════════ M4（D5）：搜索整体移交给子 agent ══════════
    # 主 agent 的工具集里**没有 search_poi** —— 留着它，模型永远直搜
    # （一步到位 token 更少），task 成摆设，上下文隔离名存实亡。
    # 子 agent 内部复用同一个 search_poi 实例：在 tool_step 的池子作用域内
    # 执行，搜到的 POI 自动进主候选池，封闭世界不破（见 subagent.py docstring）。
    subgraph = build_search_subagent(llm=build_sub_llm(model), search_tool=by_name["search_poi"])
    main_tools = [
        build_task_tool(subgraph=subgraph, default_city=default_city),
        by_name["get_weather"],
        by_name["calc_distance"],
    ]

    return Nodes(
        provider=active_provider,
        tools=main_tools,
        llm_tool=build_tool_llm(model),
        llm_plan=build_plan_llm(model),
        llm_extract=build_extract_llm(model),
        llm_soft=build_soft_llm(model),
        llm_skel=build_skel_llm(model),
        today=today,
    )


def build_graph(nodes: Nodes, checkpointer: Any | None = None) -> CompiledStateGraph:
    """把 10 个节点和边接起来。

    条件边的路由函数**定义在这里而不是 nodes.py**：
    路由是"图的形状"，属于本文件；节点是"一步做什么"，属于 `nodes.py`。
    混在一起会让"这个图长什么样"要在两个文件之间来回翻。

    `checkpointer`：M6 chat 传 `AIOMySQLSaver`（按 `thread_id=session_id` 存取，
    同一会话多轮对话靠它续上下文）；CLI / 测试不传（无跨轮状态）。
    """

    # ══════════════════════════════════════════════════════════
    #  路由（全部是纯函数：只读 state，不产生副作用）
    # ══════════════════════════════════════════════════════════

    def after_intent(state: TripState) -> str:
        """缺目的地/日期 → 追问；否则先快速排骨架再开工（D77）。

        ⚠️ 只问**阻塞项**（A25）。`missing_required` 由 `parse_intent` 用
        `Requirements.missing_blocking()` 算出来，**永远只含那两个字段**。
        用"7 项缺任何一项都追问"会让 agent 卡死在用户不想回答的预算上。
        """
        return "ask_more" if (state.get("missing_required") or []) else "make_skeleton"

    def after_agent(state: TripState) -> str:
        """模型还要调工具吗？

        ⚠️ **这里刻意不做预算判断**（不判 `tool_call_count` / `agent_rounds`）。
        一开始写了，测试立刻发现它让 `FORCE_STOP_TEXT` **永远不会产生**：
        条件边抢在 `agent_step` 之前就决定去 `generate_plan`，
        于是"已达上限，停止查询"这条消息根本发不出来，
        用户看到的是行程莫名少了几站。

        "超限就不再调工具"这个保证来自 `agent_step` 的守卫，而且**只能来自那里**：
        守卫触发时它返回一条**纯文本**消息（没有 `tool_calls`），
        条件边看到没有 `tool_calls` 自然就去生成节点了 —— 不需要重复判一遍。

        两处判据重复的代价不只是啰嗦：**它是两个必须同时改的地方**，
        而漏改其中一处的表现是"守卫静默失效"，不会报错。
        """
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        calls = list(getattr(last, "tool_calls", None) or [])
        return "tool_step" if calls else "generate_plan"

    def after_check(state: TripState) -> str:
        """有硬错就打回，重排满 2 轮就降级输出。

        ⚠️ 判据是 `check_rounds` = **已经重排过几轮**（由 `repair` 累加），
        **不是"被打回了几次"**。这个区别有实际后果：

        | 计数器语义 | 上限 2 时的行为 |
        |---|---|
        | 打回次数（check_plan 累加） | 第 2 次打回时直接放弃 → **只重排了 1 次** |
        | ✅ 重排轮数（repair 累加） | 重排 2 次，plan 共跑 3 次 |

        后者才是 D25 说的"L3 = 2 轮"。前者会让"上限 2"实际退化成"上限 1"。

        ⚠️ **两个出口都去 `soft_check`，不是 `render`** —— 软判据要写在**定稿的那一版**上。
        如果让 `repair` 分支绕过它，"打回 2 轮后降级输出"的行程就一条软提醒都没有，
        而那恰恰是最需要提醒的行程（它有硬错没修好）。
        """
        if not (state.get("blocking") or []):
            return "soft_check"
        if (state.get("check_rounds") or 0) >= nodes.max_check_rounds:
            # 🔴 达上限 = **降级输出**，不是失败。剩余风险已经写进
            #    `trip.validation.remaining`，前端会显示为灰色/红色标记。
            #    这里如果改成 raise，用户会拿不到任何东西，比"带着风险的行程"更糟。
            return "soft_check"
        return "repair"

    # ══════════════════════════════════════════════════════════
    #  组装
    # ══════════════════════════════════════════════════════════

    builder = StateGraph(TripState)

    builder.add_node("parse_intent", nodes.parse_intent)
    builder.add_node("ask_more", nodes.ask_more)
    builder.add_node("make_skeleton", nodes.make_skeleton)
    builder.add_node("agent_step", nodes.agent_step)
    builder.add_node("tool_step", nodes.tool_step)
    builder.add_node("generate_plan", nodes.generate_plan)
    builder.add_node("check_plan", nodes.check_plan)
    builder.add_node("soft_check", nodes.soft_check)
    builder.add_node("repair", nodes.repair)
    builder.add_node("render", nodes.render)

    builder.set_entry_point("parse_intent")

    builder.add_conditional_edges(
        "parse_intent",
        after_intent,
        {"ask_more": "ask_more", "make_skeleton": "make_skeleton"},
    )
    builder.add_edge("make_skeleton", "agent_step")  # ← 骨架发完事件就进工具循环（D77）
    builder.add_conditional_edges(
        "agent_step",
        after_agent,
        {"tool_step": "tool_step", "generate_plan": "generate_plan"},
    )
    builder.add_edge("tool_step", "agent_step")  # ← L2 的回边
    builder.add_edge("generate_plan", "check_plan")
    builder.add_conditional_edges(
        "check_plan",
        after_check,
        {"repair": "repair", "soft_check": "soft_check"},
    )
    builder.add_edge("soft_check", "render")  # ← 软判据只写提醒，无条件往下走
    builder.add_edge("repair", "agent_step")  # ← L3 的回边（回到 L2，不是回生成）
    builder.add_edge("ask_more", END)
    builder.add_edge("render", END)

    return builder.compile(checkpointer=checkpointer)


# ══════════════════════════════════════════════════════════════
#  跑一次（M1 的命令行入口，M6 的 SSE 后台任务也用它）
# ══════════════════════════════════════════════════════════════


def initial_state(
    message: str,
    *,
    session_id: str | None = None,
    pending_messages: list[str] | None = None,
) -> TripState:
    """构造图入口的初始 state。

    做成函数而不是让调用处手写 dict，是因为**漏字段不会报错**：
    `TripState` 是 `total=False`，少写一个 `tool_call_count` 就是少一个上限守卫，
    图照样跑，只是在超限时不再刹车。这类 bug 不会崩，只会悄悄多花钱。
    """
    state: TripState = {
        "session_id": session_id or "",
        "user_message": message,
        # 第一条就是用户这句话。`add_messages` reducer 会把它追加进空列表；
        # 之所以不直接 `"messages": []` 之后再赋值，是因为 state 的形状
        # 在同一个字面量里一眼看完，比"先声明后修改"更不容易漏字段。
        "messages": [HumanMessage(content=message)],
        "collected_pois": {},
        "collected_weather": {},
        "tool_call_count": 0,
        "agent_rounds": 0,
        "check_rounds": 0,
        "subagent_trace": [],
        "pending_messages": list(pending_messages or []),
        "requirements": {},
        "missing_required": [],
        "blocking": [],
    }
    return state


async def run_once(
    message: str,
    *,
    session_id: str | None = None,
    pending_messages: list[str] | None = None,
    nodes: Nodes | None = None,
    config: RunnableConfig | None = None,
) -> TripState:
    """跑完一次完整流程，返回终态。

    ⚠️ **不在外面包 `poi_pool_scope`** —— 池子的作用域在 `tool_step` 内部，
    由它自己开、自己取快照存 state。外面再包一层会给人
    "池子在这个函数里有效"的错觉，而它是空的（工具只在 `tool_step` 里跑）。
    """
    runtime = nodes or build_runtime()
    graph = build_graph(runtime)
    run_config: RunnableConfig = {"recursion_limit": RECURSION_LIMIT, **(config or {})}
    return await graph.ainvoke(  # type: ignore[return-value]
        initial_state(message, session_id=session_id, pending_messages=pending_messages),
        run_config,
    )


__all__ = [
    "MAX_AGENT_ROUNDS",
    "MAX_CHECK_ROUNDS",
    "MAX_TOOL_CALLS",
    "RECURSION_LIMIT",
    "build_graph",
    "build_runtime",
    "initial_state",
    "run_once",
]
