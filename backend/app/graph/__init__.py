"""Agent 引擎（LangGraph 手写 StateGraph）。

| 文件 | 管什么 |
|---|---|
| `state.py` | 图里流转什么（以及哪些字段必须带 reducer） |
| `intent.py` | 用户那句话 → 结构化的「必问 7 项」 |
| `draft.py` | 模型出骨架、代码补事实（**防止编造的那一刀**） |
| `nodes.py` | 8 个节点各做什么 |
| `graph.py` | 这 8 个怎么接起来（**所有条件边都在这里**） |

入口是 `graph.run_once()`。要看图长什么样，只看 `graph.py` 顶部的拓扑图。
"""

from app.graph.graph import (
    RECURSION_LIMIT,
    build_graph,
    build_runtime,
    initial_state,
    run_once,
)
from app.graph.intent import BLOCKING_FIELDS, REQUIRED_FIELDS, Requirements
from app.graph.nodes import MAX_AGENT_ROUNDS, MAX_CHECK_ROUNDS, MAX_TOOL_CALLS, Nodes
from app.graph.state import TripState

__all__ = [
    "BLOCKING_FIELDS",
    "MAX_AGENT_ROUNDS",
    "MAX_CHECK_ROUNDS",
    "MAX_TOOL_CALLS",
    "RECURSION_LIMIT",
    "REQUIRED_FIELDS",
    "Nodes",
    "Requirements",
    "TripState",
    "build_graph",
    "build_runtime",
    "initial_state",
    "run_once",
]
