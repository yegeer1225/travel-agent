"""chat 的流式运行器：`astream_events` → 9 种 SSE 事件（M6 核心）。

═══════════════════════════════════════════════════════════════
 为什么要单独一层（路由不直接消费 astream_events）
═══════════════════════════════════════════════════════════════

路由层要管的是 HTTP 的事（限流 / 归属 / 落库 / 帧格式），
而「LangGraph 的内部事件流怎么翻译成前端事件」是一套独立的映射规则，
测试时还要**整体替换**（tests 注入 fake runner，不连 LLM）。
两层分开，路由测试不需要图，图测试不需要 HTTP。

═══════════════════════════════════════════════════════════════
 事件映射规则（照 `docs/api.md` 3.7 的典型顺序）
═══════════════════════════════════════════════════════════════

| LangGraph 事件 | SSE 事件 | 过滤条件 |
|---|---|---|
| `on_chain_start/end`（name 在节点表里） | `node` start/end | 排除子 agent 内部节点（不在表里自然排除） |
| `on_tool_start/end/error` | `tool_call` / `tool_result` | **只报顶层**：`metadata.langgraph_node == "tool_step"`。子 agent 内部的 search_poi 不报（那是 task 的实现细节，过程记录在 `subagent_trace`） |
| `soft_check` 节点结束 | `check` | 校验卡在这里发（而非 check_plan）—— 因为 soft_warnings 到这一刻才齐 |
| `ask_more` 节点结束 | `token`（追问文本） | ask 是模板文本，整段一发 |
| 流结束且有 trip | `token`（总结）+ `trip` + `done` | 总结是模板不是 LLM —— 不为一句客套话多花一次调用 |
| 流结束且只有 ask | `token` + `done` | 必问项缺口路径 |
| state 里有 error / 异常 | `error` + `done` | **error 之后必发 done**：前端靠 done 收 loading，EOF 没 done 会被当成断流 |

`token` 只在"出文本给用户"时发 —— 工具循环里的模型调用不流式，
所以 `agent_step` 期间没有任何 token，前端靠 node/tool_call 事件动起来（契约明文如此）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from app.graph.graph import RECURSION_LIMIT, initial_state
from app.graph.validate import flatten_checks as _flatten_checks  # M7 上移公共化
from app.schemas import (
    Check,
    CheckEvent,
    DoneEvent,
    ErrorEvent,
    NodeEvent,
    SSEEventType,
    ToolCallEvent,
    ToolResultEvent,
    TokenEvent,
    Trip,
    TripEvent,
)

# ── 节点 → 中文标签（前端直接显示，不让前端自己维护映射）──
NODE_LABELS: dict[str, str] = {
    "parse_intent": "正在理解你的需求",
    "ask_more": "整理追问",
    "agent_step": "规划下一步",
    "tool_step": "执行查询",
    "generate_plan": "生成行程草稿",
    "check_plan": "检查行程可行性",
    "soft_check": "软性检查（预约 · 排队 · 适老）",
    "repair": "修正行程",
    "render": "整理输出",
}

# ── 工具 → 动作词（tool_call 的 label 用）──
_TOOL_VERB = {"task": "正在收集候选", "search_poi": "正在搜索", "get_weather": "正在查天气", "calc_distance": "正在算车程"}

# 心跳间隔（契约：15s）。取 14s 留出余量，防止代理在整 15s 处掐线。
HEARTBEAT_SECONDS = 14.0


def _is_top_level_tool(ev: dict[str, Any], tool_step_run_id: str | None) -> bool:
    """这条 tool 事件是不是"模型在 tool_step 里发起的那次调用"（而非子 agent 内部搜索）。

    判据 = 直接父是 tool_step 的节点 run。子 agent 内部的 search_poi
    挂在 task 工具 run 之下，父不同 —— 无论 metadata 怎么传导都不会误报。
    """
    if tool_step_run_id is None:
        return False
    parents = ev.get("parent_ids") or []
    return bool(parents) and parents[-1] == tool_step_run_id


def _tool_label(tool: str, args: dict[str, Any]) -> str:
    """tool_call 的 label：把最有信息量的参数拼进去（"正在搜索：武侯祠"）。"""
    verb = _TOOL_VERB.get(tool, "正在调用")
    detail = ""
    if tool in {"task", "search_poi"}:
        detail = str(args.get("keyword") or args.get("objective") or "")
    elif tool == "get_weather":
        detail = str(args.get("date_str") or "")
    elif tool == "calc_distance":
        detail = f"{args.get('from_poi_id', '')}→{args.get('to_poi_id', '')}"
    return f"{verb}：{detail}" if detail else verb


def _tool_summary(output: Any) -> str:
    """tool_result 的 summary：**不要把原始返回丢给前端**，只给一行人话。"""
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    first_line = text.strip().splitlines()[0] if text.strip() else "（空返回）"
    return first_line[:80]


def _summarize_trip(trip: Trip) -> str:
    """行程总结的模板文本（token 事件的素材）。"""
    days = len(trip.days)
    stops = sum(len(d.stops) for d in trip.days)
    return f"行程已生成：{trip.destination} {days} 天 {stops} 站，共 {trip.summary.hard_errors} 个硬错误。"


def build_chat_input(message: str, *, session_id: str, resume: bool, has_checkpoint: bool) -> dict[str, Any] | None:
    """构造图入口的输入。

    🔴 **续轮（has_checkpoint）不带 `collected_pois` / `requirements` / `missing_required`**：
    LangGraph 的合并语义是"输入里**给了的键覆盖**，没给的键保留 checkpoint 里的"。
    第二轮用户只说"加个博物馆"，如果把这些键给了（哪怕给的是空 dict），
    第一轮攒下的候选池和目的地就会被清掉 —— parse_intent 自己会读旧 requirements
    做合并，所以这里只需要"别把旧数据冲掉"。

    `resume=True`（D27 断线恢复）返回 `None`：LangGraph 对 `input=None` 的语义就是
    "从 checkpoint 停下的地方继续"，**不重发用户消息、不重新落库**。
    """
    if resume:
        return None
    base = initial_state(message, session_id=session_id)
    if has_checkpoint:
        # 续轮：只要"输入"和"每轮清零的计数"，其余全靠 checkpoint 保留
        return {
            "session_id": base["session_id"],
            "user_message": base["user_message"],
            "messages": base["messages"],
            "tool_call_count": 0,
            "agent_rounds": 0,
            "check_rounds": 0,
        }
    return base


async def graph_chat_stream(
    graph: CompiledStateGraph,
    config: RunnableConfig,
    message: str,
    *,
    session_id: str,
    resume: bool = False,
    has_checkpoint: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """跑一轮图，吐 SSE 事件 dict（不含 `session` / 帧格式 —— 那是路由的事）。

    内部是"生产者-消费者"：图事件进 `asyncio.Queue`，消费端带 14s 超时地取，
    超时就吐一条心跳 —— 这样**图卡住时前端也能收到 `: ping`**，代理不会掐线。
    """
    chat_input = build_chat_input(message, session_id=session_id, resume=resume, has_checkpoint=has_checkpoint)
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def producer() -> None:
        try:
            # ── 本轮缓存的校验上下文：check_plan 的产物 + soft_check 的补全 ──
            pending_validation: dict[str, Any] | None = None
            pending_blocking: list[str] = []
            root_output: dict[str, Any] | None = None
            node_t0: dict[str, float] = {}  # 计时起点存这里 —— start/end 是两个事件 dict，存事件里传不过去
            # tool_step 节点 run 的 id：**顶层工具的判据是 parent_ids 的直接父**。
            # 🔴 不能用 metadata.langgraph_node 过滤 —— 工具是在节点里 `ainvoke` 的，
            # 环境回调会把节点的 config 传给**子 agent 内部的 search_poi**
            # （它继承 task 工具 run 的上下文），metadata 会一路传下去。
            # parent_ids[-1] == tool_step 的 run_id 才是真正"模型发起的那次调用"。
            tool_step_run_id: str | None = None

            async for ev in graph.astream_events(chat_input, config=config, version="v2"):
                kind = ev.get("event")
                name = ev.get("name")

                if kind == "on_chain_start" and name in NODE_LABELS:
                    if name == "tool_step":
                        tool_step_run_id = str(ev.get("run_id"))
                    node_t0[name] = time.monotonic()  # 计时起点存本地 dict（start/end 是两个事件，存事件里传不过去）
                    await queue.put(("event", NodeEvent(
                        type=SSEEventType.NODE, node=name, phase="start",
                        label=NODE_LABELS[name], elapsed_ms=None,
                    )))

                elif kind == "on_chain_end" and not ev.get("parent_ids"):
                    # 根 run 结束 = 图跑完。**用它的 output 当终态**，而不是
                    # `aget_state` —— 后者在没配 checkpointer 的图上直接抛
                    # "No checkpointer set"，CLI/测试路径会炸（实测）。
                    root_output = (ev.get("data") or {}).get("output") or {}

                elif kind == "on_chain_end" and name in NODE_LABELS:
                    t0 = node_t0.pop(name, None)
                    elapsed = int((time.monotonic() - t0) * 1000) if t0 else None
                    if name == "tool_step":
                        tool_step_run_id = None
                    await queue.put(("event", NodeEvent(
                        type=SSEEventType.NODE, node=name, phase="end",
                        label=NODE_LABELS[name], elapsed_ms=elapsed,
                    )))
                    output = (ev.get("data") or {}).get("output") or {}

                    if name == "ask_more" and output.get("ask"):
                        await queue.put(("event", TokenEvent(type=SSEEventType.TOKEN, text=str(output["ask"]))))

                    elif name == "check_plan":
                        # 缓存校验产物，等 soft_check 补上软提醒后一起发校验卡
                        pending_validation = output.get("validation")
                        pending_blocking = list(output.get("blocking") or [])

                    elif name == "soft_check" and pending_validation is not None:
                        soft = output.get("soft_report") or {}
                        trip_dump = output.get("trip") or {}
                        await queue.put(("event", CheckEvent(
                            type=SSEEventType.CHECK,
                            round=int(pending_validation.get("rounds", 0)) + 1,
                            hard_errors=len(pending_blocking),
                            # 🔴 soft_report.warnings 是 **int** 不是 list（soft.py SoftReport）
                            # —— 写成 len() 在真跑时才炸（mock fixture 按错误假设构造，pytest 没拦住）
                            soft_warnings=int(soft.get("warnings") or 0),
                            checks=_flatten_checks(trip_dump),
                        )))
                        pending_validation = None
                        pending_blocking = []

                elif kind == "on_tool_start" and _is_top_level_tool(ev, tool_step_run_id):
                    args = ev.get("data", {}).get("input") or {}
                    await queue.put(("event", ToolCallEvent(
                        type=SSEEventType.TOOL_CALL, call_id=str(ev.get("run_id")),
                        tool=str(name), args=args if isinstance(args, dict) else {},
                        label=_tool_label(str(name), args if isinstance(args, dict) else {}),
                    )))

                elif kind in {"on_tool_end", "on_tool_error"} and _is_top_level_tool(ev, tool_step_run_id):
                    if kind == "on_tool_end":
                        await queue.put(("event", ToolResultEvent(
                            type=SSEEventType.TOOL_RESULT, call_id=str(ev.get("run_id")),
                            tool=str(name), ok=True, summary=_tool_summary(ev.get("data", {}).get("output")),
                            degraded=False,
                        )))
                    else:
                        exc = ev.get("data", {}).get("error")
                        await queue.put(("event", ToolResultEvent(
                            type=SSEEventType.TOOL_RESULT, call_id=str(ev.get("run_id")),
                            tool=str(name), ok=False,
                            summary=f"调用失败：{type(exc).__name__ if isinstance(exc, BaseException) else exc}",
                            degraded=False,
                        )))

            # ── 流结束：从终态决定收尾事件 ──
            final = root_output or {}

            if final.get("error"):
                await queue.put(("event", ErrorEvent(
                    type=SSEEventType.ERROR, code="internal_error",
                    msg=str(final["error"])[:200],
                )))
            elif final.get("trip"):
                trip = Trip.model_validate(final["trip"])
                await queue.put(("event", TokenEvent(type=SSEEventType.TOKEN, text=_summarize_trip(trip))))
                await queue.put(("event", TripEvent(type=SSEEventType.TRIP, trip=trip)))
            elif final.get("ask"):
                pass  # token 已在 ask_more 节点结束时发过
            else:
                await queue.put(("event", ErrorEvent(
                    type=SSEEventType.ERROR, code="internal_error",
                    msg="流程结束但没有产出行程或追问，请重试",
                )))
            await queue.put(("state", final))  # 路由从这拿 trip_id 落库
            await queue.put(("end", None))
        except asyncio.CancelledError:
            raise  # 客户端断开 → 正常退出，producer 随消费端一起死
        except Exception as exc:  # noqa: BLE001 —— 图里抛出来的异常只能在流里报（HTTP 已 200）
            await queue.put(("event", ErrorEvent(
                type=SSEEventType.ERROR, code="internal_error",
                msg=f"{type(exc).__name__}: {exc}"[:200],
            )))
            await queue.put(("end", None))

    task = asyncio.create_task(producer())
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield {"heartbeat": True}
                continue
            if kind == "end":
                break
            if kind == "state":
                yield {"final_state": payload}
            else:
                yield {"event": payload}
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ══════════════════════════════════════════════════════════════
#  ChatHandle —— 路由拿到的"会话级句柄"（stream / steer / 生命周期）
# ══════════════════════════════════════════════════════════════


class ChatHandle:
    """一个会话的图句柄：`thread_id` 绑定 session_id，steer / 恢复都走它。

    路由对图的全部接触点收在这一个类里 —— 测试注入的 fake 句柄
    只需要实现 `has_checkpoint / steer / stream` 三个方法。

    `closer`（D59）：句柄持有的资源释放钩子（比如 checkpointer 的 MySQL 连接）。
    **请求级连接**要求流结束时必须归还 —— 客户端断开（CancelledError）与
    正常走完两条路径都要走到 `aclose`，路由的 finally 负责调它。
    """

    def __init__(
        self,
        graph: CompiledStateGraph,
        session_id: str,
        *,
        closer: Any | None = None,
    ) -> None:
        self.graph = graph
        self.session_id = session_id
        self._closer = closer
        self.config: RunnableConfig = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": RECURSION_LIMIT,
        }

    async def aclose(self) -> None:
        """释放句柄持有的资源（无 closer = no-op，测试替身不需要实现）。"""
        if self._closer is not None:
            await self._closer()

    async def has_checkpoint(self) -> bool:
        """这个会话有没有历史轮次（决定输入用合并语义还是全新初始化）。"""
        if getattr(self.graph, "checkpointer", None) is None:
            return False  # 无 checkpointer 的图（CLI/测试）永远没有历史
        snapshot = await self.graph.aget_state(self.config)
        return bool(snapshot.values)

    async def steer(self, message: str) -> bool:
        """把用户插话写进运行中会话的 `pending_messages`（agent_step 会 drain 它）。

        `pending_messages` **没有 reducer**（覆盖语义），所以这里必须
        先读当前值再追加 —— 直接写 `[message]` 会把之前没被消费的插话冲掉。
        返回 False = 流已经结束（写进了 checkpoint 但没人会读它，调用方可提示）。
        """
        if getattr(self.graph, "checkpointer", None) is None:
            return False
        snapshot = await self.graph.aget_state(self.config)
        values = snapshot.values or {}
        if not values and snapshot.next == ():
            return False
        pending = list(values.get("pending_messages") or [])
        pending.append(message)
        await self.graph.update_state(self.config, {"pending_messages": pending})
        return True

    def stream(
        self, message: str, *, resume: bool = False, has_checkpoint: bool = False
    ) -> AsyncIterator[dict[str, Any]]:
        return graph_chat_stream(
            self.graph, self.config, message,
            session_id=self.session_id, resume=resume, has_checkpoint=has_checkpoint,
        )


def make_default_chat_factory() -> Any:
    """生产用的会话句柄工厂：**每请求一条独立的 checkpointer 连接**（D59）。

    为什么不再进程级复用单连接（M6 原实现）：`AIOMySQLSaver.from_conn_string`
    内部是 `aiomysql.connect()` —— **一根单连接，不是池**。SSE 流被客户端
    中断（关页面/网络抖动/断线恢复重连）时，正在这条连接上跑的查询被
    asyncio 取消，aiomysql 把连接标记为死（"Cancelled during execution"），
    此后**所有** chat 复用这条死连接全部 500，直到重启 —— 2026-09-16 冒烟
    实测复现。单连接在"不同 session 并发 chat"下还会互相踩（两个查询交织）。

    请求级连接的代价：每次 chat 多一次 TCP 握手（本地 MySQL <1ms，chat
    本身限流 10/小时，完全可忽略）+ 每请求重建图（纯内存，毫秒级）。
    连接被打死只影响那一次请求，新请求拿新连接 —— 自愈。
    """

    state: dict[str, Any] = {"setup_done": False}

    async def factory(session_id: str, model: str | None = None) -> ChatHandle:
        """`model` = 会话级模型（A45/D55）。每请求独立连接 + 独立图实例
        （checkpoint 的 key 是 thread_id=session_id，与连接/图实例无关）。"""
        from langgraph.checkpoint.mysql.aio import AIOMySQLSaver

        from app.config import settings
        from app.graph.graph import build_graph, build_runtime

        saver_cm = AIOMySQLSaver.from_conn_string(settings.mysql_dsn_async)
        saver = await saver_cm.__aenter__()
        try:
            if not state["setup_done"]:
                await saver.setup()  # 建 checkpoint 四张表（幂等）；建过一次后续连接直接用
                state["setup_done"] = True

            async def close() -> None:
                await saver_cm.__aexit__(None, None, None)

            graph = build_graph(build_runtime(model=model), checkpointer=saver)
            return ChatHandle(graph, session_id, closer=close)
        except BaseException:
            await saver_cm.__aexit__(None, None, None)  # 建图半途失败也要还连接
            raise

    return factory


__all__ = [
    "HEARTBEAT_SECONDS",
    "NODE_LABELS",
    "ChatHandle",
    "build_chat_input",
    "graph_chat_stream",
    "make_default_chat_factory",
]
