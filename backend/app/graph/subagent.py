"""搜索子 agent（M4，D5 的落地形态：Subagents —— 主 agent 把它当工具调）。

═══════════════════════════════════════════════════════════════
 它是什么、不是什么
═══════════════════════════════════════════════════════════════

**它是一把"上下文防火墙"，不是一个更聪明的搜索。**

主 agent 每搜一次 `search_poi`，几十行候选清单就永久留在主上下文里；
搜 8 次，主 agent 的每一轮决策都背着这几百行文本走。
子 agent 把"多轮试错"（搜 → 看结果不满意 → 换词重搜）整个圈进自己的小图里，
**只有最终精选清单回流主 agent** —— 这就是 D5 面试口径里的
"不是为了并行，是为了收窄上下文"。

═══════════════════════════════════════════════════════════════
 三个必须想清楚的接线决定
═══════════════════════════════════════════════════════════════

**① 子 agent 复用同一个 `search_poi` 工具实例（不另写一份搜索逻辑）。**

`task` 工具在 `tool_step` 的 `poi_pool_scope` **内部**被执行，
子图节点里调 `search_tool.ainvoke(...)` 时，`search_poi` 内部那句
`poi_pool.record(pois)` 写的是**同一个池子对象**（contextvars 在同一
asyncio 任务链里传播，池子本身是引用共享的可变对象）。
于是子 agent 搜到的 POI **自动进主候选池** —— `generate_plan` 的封闭世界
照样能从池子里看到它们，**封闭世界不因为多了一层 agent 而破洞**。

⚠️ 这也意味着：**上下文隔离 ≠ 数据隔离**。
隔离的是"模型看到的话"（子 agent 的几十条搜索往来不进主 `messages`）；
共享的是"事实"（池子本来就该是全局的事实底座）。两者分开看，
混为一谈会得出"要给子 agent 复制一个池子"的错误结论。

**② 主 agent 的工具集里没有 `search_poi`（搜索只能走 task）。**

保留直搜的话，模型永远选一步到位的 `search_poi`（token 更少），
`task` 成摆设，隔离名存实亡，M4 的验收（trace 里能看到子 agent）
也过不了。移除是 D5 的题中之义：搜索整体移交给子 agent。

**③ 子 agent 的规划 LLM 不挂 tools（模型出意图，代码执行搜索）。**

子 agent 每轮输出一个 JSON：要么 `{action:"search", keywords:[...]}`，
要么 `{action:"final", picks:[{poi_id, reason}]}`。搜索由**代码**执行。
这比"子 agent 自己也是一个带工具的循环"便宜且稳：

| 方案 | 后果 |
|---|---|
| 子 agent 挂 tools 自己循环 | 每个关键词一次模型调用；且带 tools 就要关思考（坑 1 的纪律），关键词试错本来也用不上 CoT |
| ✅ 模型出意图、代码执行 | 一轮 = 1 次小 LLM 调用 + N 次确定性搜索；格式坏了直接降级，不会连环空转 |

═══════════════════════════════════════════════════════════════
 预算与降级（D25 精神：超限降级输出，绝不抛异常）
═══════════════════════════════════════════════════════════════

子 agent 内部自限：**规划轮 ≤ MAX_SUB_ROUNDS，搜索次数 ≤ MAX_SUB_SEARCHES**。
超限或规划 LLM 挂掉 = **强制收尾**：把池里已拿到的站点型候选不做精选直接
返回（标注"未精选"），绝不把失败变成异常抛给主图 —— 那会让主 agent 的
ToolMessage 变成报错文案，主 agent 大概率重试整个 task，白烧一轮。

⚠️ **主预算的会计口径**：`task` 在主图里计 **1 次工具调用**
（它就是一条 tool_call）。子 agent 内部的搜索**不占用**主图的
`tool_call_count` —— 工具函数写不进主 state，这是结构决定的。
最坏成本的账是算过的：主图 3 次 task × (4 轮规划 + 6 次搜索)
≈ 12 次小 LLM 调用 + 18 次 provider 调用。若实测失控，收紧的是
这里两个常量，不是去发明跨层记账。
"""

from __future__ import annotations

import contextvars
import json
from contextlib import contextmanager
from typing import Any, Iterator, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, tool
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.tools import poi_pool
from app.tools.poi_rank import is_visit_type, rank_pois
from app.utils import text_of

MAX_SUB_ROUNDS = 4
"""子 agent 的**规划 LLM 轮数**上限。含收尾那一轮 —— 最坏 = 3 次搜索规划 + 1 次终选。"""

MAX_SUB_SEARCHES = 6
"""子 agent 内部**搜索次数**上限（每个关键词算一次）。
一次 task 给 6 个关键词已经很宽裕：正常用法是 2~4 个。"""

SUB_SYSTEM_PROMPT = """你是搜索规划员。主规划师交给你一个找地点的任务，你来完成搜索并给出精选清单。

你每一轮输出一个 JSON 对象（不要 markdown 代码块），二选一：

① 还需要搜索：
{"action": "search", "keywords": ["关键词1", "关键词2"]}

② 搜索够了，给出精选：
{"action": "final", "picks": [{"poi_id": "结果清单里的id", "reason": "为什么推荐它（一句话）"}]}

规则：
1. 每轮给 1~2 个**具体的**关键词（例如「都江堰」「成都 川菜馆」），
   不要给「美食」「景点」这类太泛的词 —— 泛词返回的多是无关店铺。
2. 已经搜过的关键词**不要重复给**，换角度换词。
3. final 的 `poi_id` **只能来自搜索结果里出现过的 id**。
   一个都没搜到就给空 picks，不要编。
4. picks 按推荐程度排序，给 3~8 条，每条都要有一句话理由。"""


class SubState(TypedDict, total=False):
    """子图的私有状态。**与主图 `TripState` 完全隔离** ——
    子 agent 的中间过程只活在这份结构里，跑完就丢。"""

    objective: str
    city: str
    notes: list[str]
    """每轮搜索结果的窗口文本。这是子 agent 自己的"上下文"。"""
    keywords: list[str]
    """已经搜过的关键词（避免重复 + trace 用）。"""
    new_keywords: list[str]
    """**本轮规划刚决定要搜的**关键词。`sub_search` 只搜这些、搜完清空。

    🔴 为什么单开一个字段而不是让 `sub_search` 自己取 `keywords[-2:]`：
    `keywords` 是**累计**的（去重 + trace 都要用），而 `[-2:]` 取到的是
    "最近两轮的词" —— 那样每一轮都会把上一轮搜过的**再搜一遍**
    （实测：同一个词连烧 3 次搜索配额，高德还有限流）。规划声明、
    搜索执行、各管各的，才不会重复劳动。
    """
    search_calls: int
    rounds: int
    """规划 LLM 已跑的轮数（含收尾轮）。"""
    picks: list[dict[str, Any]]
    """出现这个键 = 规划宣布收尾（哪怕是空的 —— "明确没找到"也是结论）。"""
    final_text: str
    """回流主 agent 的最终文本。主上下文里只会有它。"""
    degraded: bool
    llm_failed: bool


# ══════════════════════════════════════════════════════════════
#  trace 的传递通道（与 poi_pool 同款：contextvars）
# ══════════════════════════════════════════════════════════════

_trace_var: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "subagent_trace", default=None
)


@contextmanager
def subagent_trace_scope() -> Iterator[None]:
    """开一段 trace 收集作用域。`tool_step` 执行工具前开、结束后 drain。

    为什么和池子用同一个办法：工具函数（包括 task）拿不到主图 state，
    而它产出的过程记录必须**有人带回 state**。池子已经用 contextvars
    走通了这条路，trace 复用同一个模式 —— 不发明第二种机制。
    """
    token = _trace_var.set([])
    try:
        yield
    finally:
        _trace_var.reset(token)


def drain_subagent_trace() -> list[dict[str, Any]]:
    """取走作用域内收集的全部 trace 记录（取完即清，防止串到下一次）。

    ⚠️ 作用域外调用**必须是无操作**，不能顺手 `set([])`：
    那会在当前上下文里留下一个没人管理的泄漏列表 —— 后续所有子图
    的记录都往里堆、永远没人来取（跨请求的 trace 污染 + 内存滞留）。
    "没有作用域"和"作用域是空的"要严格区分。
    """
    records = _trace_var.get()
    if records is None:
        return []
    _trace_var.set([])
    return records


def _record_trace(entry: dict[str, Any]) -> None:
    scope = _trace_var.get()
    if scope is not None:
        scope.append(entry)


# ══════════════════════════════════════════════════════════════
#  终选与降级
# ══════════════════════════════════════════════════════════════


def _fallback_candidates(pool: poi_pool.PoiPool | None) -> list[dict[str, str]]:
    """降级终选：把池里"能当站点"的条目按重排顺序端出来，不做精选。

    ⚠️ 池子是主图级的，里面可能有**上一个 task** 搜的 —— 这不是错：
    子 agent 无状态（D5 已接受），降级清单本来就允许"把池里能用的都端上来"，
    主 agent 会在 `generate_plan` 里做真正的挑选。这里用 `rank_pois` 排序，
    站点型（`is_visit_type`）排前面、非站点的被滤掉。
    """
    if not pool:
        return []
    picks: list[dict[str, str]] = []
    for poi in rank_pois(pool.all()):
        if not is_visit_type(poi.type):
            continue
        picks.append({"poi_id": poi.poi_id, "reason": ""})
        if len(picks) >= 8:
            break
    return picks


def _render_result(
    picks: list[dict[str, Any]],
    pool: poi_pool.PoiPool | None,
    *,
    degraded: bool,
    state: SubState,
) -> tuple[str, int]:
    """终选清单 → 回流主 agent 的文本。

    picks 里的 `poi_id` **逐个过池子**：不在池里的 = 模型凭记忆编的，
    直接丢弃（封闭世界在子 agent 出口再守一道 —— 它守住了
    "子 agent 模型编 id"这最后一层，主图的 `assemble_blocking`
    就永远不需要为 task 的产物买单）。

    返回 (文本, 通过条数) —— 条数进 trace，不靠数换行符（那在
    "没有找到候选"的分支会算出负数之类的鬼数字）。
    """
    lines: list[str] = [f"【搜索完成】任务：{state.get('objective', '')}"]
    if degraded:
        lines.append("（搜索预算已用完或搜索规划失败，以下是未经精选的全部可用候选）")

    kept = 0
    for pick in picks:
        poi_id = str(pick.get("poi_id") or "").strip()
        reason = str(pick.get("reason") or "").strip()
        poi = pool.get(poi_id) if pool else None
        if poi is None:
            continue
        parts = [poi.poi_id, poi.name]
        if poi.rating:
            parts.append(f"评分{poi.rating}")
        parts.append(f"开放{poi.open_time}" if poi.open_time else "开放时间未知")
        if reason:
            parts.append(f"理由：{reason}")
        lines.append(f"{kept + 1}. " + " | ".join(parts))
        kept += 1

    if kept == 0:
        lines.append(
            "没有找到合适的候选。不要凭记忆填任何地点；"
            "如果这个方向确实没有结果，就少安排相关站点，或换一个任务描述重新派搜索。"
        )
    else:
        lines.append(f"（共 {kept} 条，子 agent 搜索 {state.get('search_calls', 0)} 次）")
    return "\n".join(lines), kept


# ══════════════════════════════════════════════════════════════
#  子图组装
# ══════════════════════════════════════════════════════════════


def build_search_subagent(
    *,
    llm: BaseChatModel,
    search_tool: BaseTool,
) -> CompiledStateGraph:
    """组装搜索子图。**主图启动时编译一次**，多次 task 调用复用同一份图。

    `llm` 用 extract 档（关思考 + temperature=0）：关键词选择是"文本里
    有什么就取什么"一类的工作，不需要 CoT；要的是可复现。
    `search_tool` 是主图同款 `search_poi` 工具实例 —— 见模块 docstring ①。
    """

    # ── 节点 1：规划（模型出意图）──
    async def sub_plan(state: SubState) -> SubState:
        rounds = state.get("rounds", 0)
        notes = state.get("notes", [])
        keywords = state.get("keywords", [])

        blocks = [
            f"# 任务（城市：{state.get('city', '') or '未指定'}）",
            state.get("objective", ""),
            "",
            "# 已搜过的关键词（不要重复）",
            "、".join(keywords) if keywords else "（无）",
            "",
            "# 搜索结果",
            *(notes if notes else ["（还没有搜过任何东西）"]),
        ]

        parsed: dict[str, Any] | None = None
        try:
            response = await llm.bind(response_format={"type": "json_object"}).ainvoke(
                [SystemMessage(content=SUB_SYSTEM_PROMPT), HumanMessage(content="\n".join(blocks))]
            )
            candidate = json.loads(text_of(response))
            parsed = candidate if isinstance(candidate, dict) else None
        except Exception:  # noqa: BLE001 —— LLM 挂了 / 输出坏了都走强制收尾
            parsed = None

        action = str((parsed or {}).get("action") or "")
        budget_over = rounds + 1 >= MAX_SUB_ROUNDS

        if action == "final" or budget_over:
            picks = parsed.get("picks") if (parsed and action == "final") else None
            return {
                "rounds": rounds + 1,
                # 🔴 预算超限的强制收尾必须标 degraded：picks 是空的（模型没给），
                #    但语义是"预算用尽"不是"没找到" —— 前者要给降级清单，
                #    后者要如实说没有。两者的出口都在 finalize，靠这个标记分流。
                "picks": [p for p in (picks or []) if isinstance(p, dict)],
                "degraded": budget_over,
                "llm_failed": parsed is None,
                "new_keywords": [],  # 收尾轮清空，别让残留的本轮词被误搜
            }

        new_keywords = [str(k).strip() for k in (parsed.get("keywords") or []) if str(k).strip()]
        new_keywords = [k for k in new_keywords if k not in keywords]
        return {
            "rounds": rounds + 1,
            "keywords": [*keywords, *new_keywords],
            "new_keywords": new_keywords,
        }

    # ── 节点 2：执行搜索（代码，确定性）──
    async def sub_search(state: SubState) -> SubState:
        city = state.get("city", "")
        # 只搜**本轮新增**的（见 SubState.new_keywords 的注释），一轮最多 2 个
        keywords = [k for k in (state.get("new_keywords") or []) if k][:2]
        notes = list(state.get("notes", []))
        calls = state.get("search_calls", 0)

        for keyword in keywords:
            if calls >= MAX_SUB_SEARCHES:
                break
            try:
                result = await search_tool.ainvoke({"keyword": keyword, "city": city})
            except Exception as exc:  # noqa: BLE001 —— 单个关键词失败不连坐
                notes.append(f"关键词「{keyword}」搜索失败：{type(exc).__name__}")
                continue
            notes.append(f"## 关键词「{keyword}」\n{result}")
            calls += 1

        update: SubState = {"notes": notes, "search_calls": calls, "new_keywords": []}
        # 搜索预算用尽就别再回规划节点空转了：LLM 看到同样的结果只会再给
        # 一批同样搜不动的新词，直到轮数超限被强制收尾 —— 那是白烧的 LLM 调用。
        if calls >= MAX_SUB_SEARCHES:
            update["picks"] = []
            update["degraded"] = True
        return update

    # ── 节点 3：终选（过池子、渲染回流文本、记 trace）──
    async def sub_finalize(state: SubState) -> SubState:
        pool = poi_pool.current_pool()
        picks = list(state.get("picks") or [])
        degraded = bool(state.get("degraded"))

        if not picks and not degraded and state.get("notes"):
            # 模型给了 final 但 picks 为空：它明确说"没有合适的"。尊重这个结论，
            # 不用降级清单覆盖 —— 主 agent 会看到"没找到"并自己决定怎么办。
            final_text, kept = _render_result([], pool, degraded=False, state=state)
        else:
            if not picks:
                picks = _fallback_candidates(pool)
                degraded = True
            final_text, kept = _render_result(picks, pool, degraded=degraded, state=state)

        _record_trace(
            {
                "objective": state.get("objective", ""),
                "city": state.get("city", ""),
                "rounds": state.get("rounds", 0),
                "searches": state.get("search_calls", 0),
                "keywords": list(state.get("keywords", [])),
                "returned": kept,
                "degraded": degraded,
                "llm_failed": bool(state.get("llm_failed")),
            }
        )
        return {"final_text": final_text}

    def after_plan(state: SubState) -> str:
        """有 `picks` 键（含空 —— "明确没找到"也是结论）→ 终选；否则搜索。"""
        return "sub_finalize" if "picks" in state else "sub_search"

    builder = StateGraph(SubState)
    builder.add_node("sub_plan", sub_plan)
    builder.add_node("sub_search", sub_search)
    builder.add_node("sub_finalize", sub_finalize)
    builder.set_entry_point("sub_plan")
    builder.add_conditional_edges(
        "sub_plan", after_plan, {"sub_search": "sub_search", "sub_finalize": "sub_finalize"}
    )
    builder.add_edge("sub_search", "sub_plan")  # 子 agent 自己的小循环
    builder.add_edge("sub_finalize", END)
    return builder.compile()


def build_task_tool(
    *,
    subgraph: CompiledStateGraph,
    default_city: str,
) -> BaseTool:
    """把子图包成主 agent 可调的 `task` 工具。

    ⚠️ docstring 就是 prompt（见 `amap_tools.py` 坑 ③）：
    "必须自包含"这条约束写在**参数说明**里 —— 模型填参数时读到的正是它。
    """

    @tool
    async def task(objective: str, city: str = "") -> str:
        """派出搜索子 agent 去找地点。它会自己多轮搜索、换关键词试错，
        最后返回一份**带 id 的精选候选清单**。

        排行程需要的所有地点都必须来自这个工具（或距离工具算过的地点）的返回。

        Args:
            objective: 找什么。**必须自包含**（子 agent 看不到本对话的其他内容）：
                写清城市、要哪类地点、有什么约束。
                例如「成都适合带老人慢走的景点，步道平缓有休息点」。
            city: 城市名，例如"成都"。留空则用本次行程的目的地。
        """
        if not objective.strip():
            return "任务描述是空的，没法搜索。请写清楚要找什么（城市 + 哪类地点 + 约束）。"

        result = await subgraph.ainvoke(
            {
                "objective": objective.strip(),
                "city": city.strip() or default_city,
                "notes": [],
                "keywords": [],
                "new_keywords": [],
                "search_calls": 0,
                "rounds": 0,
            }
        )
        return str(result.get("final_text") or "")

    return task


__all__ = [
    "MAX_SUB_ROUNDS",
    "MAX_SUB_SEARCHES",
    "build_search_subagent",
    "build_task_tool",
    "drain_subagent_trace",
    "subagent_trace_scope",
]
