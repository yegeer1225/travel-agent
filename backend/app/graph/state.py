"""图的状态定义。

═══════════════════════════════════════════════════════════════
 为什么字段类型大多是 `dict` 而不是 Pydantic 对象
═══════════════════════════════════════════════════════════════

**因为 state 要进 checkpoint 被序列化。**（M5 接 MySQL checkpointer 时兑现）

LangGraph 的 checkpointer 会把整个 state 编解码（msgpack），
放进去的对象必须能稳定往返。Pydantic 对象在很多序列化器下会变成 dict 再回来，
于是"进去是 `Trip`、出来是 `dict`" —— 这种**类型漂移不报错**，只在某处 `.status`
取不到值时才炸（和坑 15 / 坑 21 同一类病）。

所以约定：**state 里只放可序列化的朴素结构**，节点内部要用时再 `Model.model_validate()`。
代价是每个节点多一行校验；收益是**序列化往返后行为完全一致**。

═══════════════════════════════════════════════════════════════
 两个 reducer 的作用（不写就会静默出错）
═══════════════════════════════════════════════════════════════

| 字段 | reducer | 为什么不写会错 |
|---|---|---|
| `messages` | `add_messages` | 默认是**覆盖**。节点返回新的 tool 消息时会把整个历史换掉，图立刻失忆 |
| `tool_call_count` | `operator.add` | 默认是**覆盖**。计数永远等于"最后一次返回的值"，上限守卫（D25 的 L2≤8）形同虚设 |

⚠️ `pending_messages` **刻意不加 reducer** —— 它要的就是覆盖语义：
drain 之后返回 `[]` 就是清空。加了 `add` 反而永远清不掉。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class TripState(TypedDict, total=False):
    """一次请求内部流转的全部状态。

    `total=False` 是刻意的：不是每个字段在每一跳都存在，
    强行要求全部必填会逼出一堆占位默认值，而那些默认值会掩盖"这步没跑"的事实。
    """

    # ══════════ 输入（图的入口写） ══════════
    session_id: str
    user_message: str
    """用户这一轮说的话。**只承载"输入"，不承载历史** ——
    历史在 `messages` 里，两者混在一起会让"这一轮用户到底说了什么"变得要翻找。"""

    # ══════════ 需求（parse_intent 写） ══════════
    requirements: dict[str, Any]
    """`Requirements` 的 dump。含目的地 / 日期 / 天数 / 同行人 / 预算 / 偏好 / 特别要求（A25）。"""

    missing_required: list[str]
    """缺失的**阻塞项**。A25 定死：7 项必问里**只有「目的地」和「日期」阻塞流程**。
    非空 → 走 `ask_more`，不进工具循环。"""

    # ══════════ L2 工具循环 ══════════
    messages: Annotated[list[AnyMessage], add_messages]
    """对话消息序列（含 `tool_calls` 与 `ToolMessage`）。
    ⚠️ 这是**模型看到的上下文** —— 工具返回的原始文本都在里面。"""

    tool_call_count: Annotated[int, operator.add]
    """累计工具调用次数。**用 `operator.add` 累加**（见模块 docstring 的表格）。
    D25：单次行程上限 20；L2 单节点上限 8。"""

    agent_rounds: Annotated[int, operator.add]
    """`agent_step` 被执行的次数。**D25 的 L2 上限 8 指的是这个** —— 不是工具调用次数。

    ⚠️ **两者不能合并成一个计数**：模型一轮里可能发起 3 个 `tool_calls`
    （一次搜三个关键词），也可能一轮只发 1 个。用"工具调用次数 ≤20"
    当 L2 上限，会让"每轮只调 1 个工具"的情况多跑 12 轮；
    用"轮数 ≤8"当总上限，又会漏掉"单轮 8 个调用"的爆量。
    两个限制管的是不同的失控方式：**偶发多调 vs 反复空转**。
    """

    pending_messages: list[str]
    """用户插队消息（steering）。**不加 reducer** —— 要的就是覆盖语义，
    `drain_pending_messages` 返回 `[]` 即清空。"""

    # ══════════ 事实快照 ══════════
    collected_pois: dict[str, dict[str, Any]]
    """本轮工具**真实返回过**的 POI（id → dump）。

    ⚠️ **这是 `poi_pool` 的持久化副本**，为什么要存两份：
    运行时的池子在 `contextvars` 里（因为**工具函数拿不到 state**，见 `tools/poi_pool.py`），
    而 `contextvars` **不会被 checkpoint 保存** —— 进程重启/断线恢复后就空了。
    所以 `tool_step` 每次把池子快照写进这里，图入口再把快照灌回池子。

    M1 阶段它只是快照；**M5 接 checkpointer 时它就是"恢复后还能校验"的唯一依据**。
    """

    collected_weather: dict[str, dict[str, Any]]
    """日期 → 天气 dump。同样为了恢复。"""

    # ══════════ 产物 ══════════
    draft: dict[str, Any]
    """模型出的**精简草稿**（只有 station 骨架，见 `graph/draft.py`）。
    为什么要中间层：让模型一次输出完整 `Trip`（含 checks / 距离 / 坐标）必然会瞎编 ——
    那些字段**本该由代码算**。模型只做判断（去哪、待多久、为什么），代码做确定性补全。"""

    trip: dict[str, Any]
    """补全后的完整 `Trip` dump。**这是交给前端的那个结构**。"""

    check_rounds: Annotated[int, operator.add]
    """**已经重排过几轮**（由 `repair` 累加），D25 上限 2。

    ⚠️ 语义是"重排轮数"**不是"被打回了几次"** —— 两者差一轮，
    而这个差别会让"上限 2"实际退化成"上限 1"。理由见 `graph.py` 的 `after_check`。
    """

    # ══════════ 校验 ══════════
    validation: dict[str, Any]
    """`Validation` dump —— 含 `fixed` 与 `remaining`（三态）。"""

    blocking: list[str]
    """**硬错**列表。非空 → `repair`；连续 2 轮仍非空 → 降级输出（D25）。"""

    repair_hint: str
    """上一次被打回的**原因文本**，回灌给生成节点。

    ⚠️ 为什么要单独一个字段而不是让生成节点去读 `messages`：`generate_plan`
    **刻意不读对话历史**（它只看"需求 + 候选清单"，避免同一份 POI 信息
    在历史里和清单里出现两次、模型分不清哪份权威）。
    所以打回原因必须走这条专用通道，而不是塞在它读不到的地方。
    """

    # ══════════ 出口 ══════════
    ask: str
    """`ask_more` 要问用户的话。"""

    error: str
    """未预期的错误。**不抛异常而是写进这里** —— 抛出去会让已经流出的 SSE 内容变成孤儿。"""


__all__ = ["TripState"]
