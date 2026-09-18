"""测试替身：脚本化的假 LLM。

═══════════════════════════════════════════════════════════════
 为什么必须用假 LLM 而不是打真实 DeepSeek
═══════════════════════════════════════════════════════════════

图这一层的测试要盯的是**流程**：循环跑不跑得出去、超限会不会刹车、
插话会不会被吃掉、打回会不会回到 L2。

这些跟模型聪不聪明**完全无关**。用真模型测它们，代价是：

| | 真模型 | 假模型 |
|---|---|---|
| 耗时 | 每次几十秒 | 毫秒 |
| 花钱 | 每次几毛 | 0 |
| **可复现** | ❌ 同样的输入可能给不同结果 | ✅ 完全确定 |
| 能构造错误场景 | ❌ 你没法要求它"故意编一个假 id" | ✅ 想让它编它就编 |

最后一行是关键：**"模型编造"这个场景必须能稳定构造**，
否则"封闭世界拦得住编造"这条断言就是靠运气在过。
真模型 99% 的时候不编，测试就会一直是绿的 —— 只在它真编的那天红一次。

所以：**假 LLM 测流程，真 LLM 测效果**（后者是 `eval/` 的事，不是 pytest 的事）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr

from app.graph.nodes import Nodes
from app.providers.mock import MOCK_CITY, MOCK_POI_POOL, MockAmapProvider
from app.tools.amap_tools import build_amap_tools


class ScriptedChatModel(BaseChatModel):
    """按脚本依次返回预设消息。脚本用完就重复最后一条。

    「用完重复最后一条」是有意的：测试里常常想让模型"一直说同一句话"
    （比如一直收工不调工具），这时脚本只写一条就够。
    """

    script: list[AnyMessage] = Field(default_factory=list)

    _cursor: int = PrivateAttr(default=0)
    _calls: list[list[AnyMessage]] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    # ---- 断言用的观察窗口 ----

    @property
    def calls(self) -> list[list[AnyMessage]]:
        """每次调用收到的完整消息列表。用来断言 prompt 里到底有没有那句话。"""
        return self._calls

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def joined_prompts(self) -> str:
        """把所有调用收到的消息拍成一整块文本，方便 `assert "xxx" in ...`。"""
        chunks: list[str] = []
        for call in self._calls:
            for msg in call:
                content = getattr(msg, "content", "")
                chunks.append(content if isinstance(content, str) else str(content))
        return "\n".join(chunks)

    # ---- 实现 ----

    def _next(self, messages: Sequence[AnyMessage]) -> AIMessage:
        self._calls.append(list(messages))
        if not self.script:
            return AIMessage(content="")
        index = min(self._cursor, len(self.script) - 1)
        self._cursor += 1
        msg = self.script[index]
        assert isinstance(msg, AIMessage)
        return msg

    def _generate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    async def _agenerate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # ⚠️ 直接实现 `_agenerate` 而**不是**靠基类委托给 `_generate`：
        # 基类的默认实现是 `run_in_executor(..., self._generate, ...)`，
        # 那会把调用丢进线程池 —— 而**线程池里 `contextvars` 不共享**。
        # 我们有好几处逻辑依赖 contextvar（poi_pool），
        # 测试环境里跑出生产环境不会有的行为，是最难查的一类假阳性。
        return self._generate(messages, stop, run_manager, **kwargs)

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChatModel:
        """原样返回自己。

        真实模型需要把 tool schema 序列化进请求；假模型不需要 ——
        它只会照着脚本回答，schema 对它没有任何影响。
        """
        return self


def ai_tool_call(name: str, args: dict[str, Any], call_id: str = "call_1") -> AIMessage:
    """造一条「模型要调工具」的消息。"""
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def ai_multi_tool_calls(*specs: tuple[str, dict[str, Any]]) -> AIMessage:
    """一条消息里带**多个** tool_calls —— 专门用来测"漏回应"那个坑。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": args,
                "id": f"call_{i}",
                "type": "tool_call",
            }
            for i, (name, args) in enumerate(specs, start=1)
        ],
    )


def ai_text(text: str = "好，我打算这么安排。") -> AIMessage:
    """造一条「模型收工了」的消息（没有 tool_calls）。"""
    return AIMessage(content=text)


# ══════════════════════════════════════════════════════════════
#  构造工具 —— `test_nodes.py` 与 `test_graph.py` 共用
#
#  放在这里而不是各自复制一份：两个文件要构造的 `Nodes` 必须**完全同构**，
#  否则"节点测试绿了但图测试红"会分不清是代码坏了还是 fixture 不一致。
# ══════════════════════════════════════════════════════════════


def run(coro):
    """跑一个协程。**不用 pytest-asyncio** —— 少一个插件依赖，
    而本项目所有异步测试都是"跑一次就完"，不需要事件循环 fixture。"""
    return asyncio.run(coro)


class BoomChatModel(BaseChatModel):
    """一调就炸的 LLM。

    用来回答一个**只有端到端才问得出来**的问题：
    "这个节点挂了，整张图还能不能走完？"
    `test_nodes.py` 里那种"直接调一个节点函数"的测法答不了它 ——
    节点自己写 `state["error"]` 和"图还继续往下走"是两回事。
    """

    _llm_type_override: str = "boom"

    @property
    def _llm_type(self) -> str:
        return "boom"

    def _generate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise RuntimeError("上游 503")


def soft_says(*findings: dict) -> AIMessage:
    """造一条软判据的模型回复。`soft_says({"code": "queue_time", ...})`。

    单独一个 helper 而不是让每个测试手写 JSON 字符串：**手写必然拼错字段名**
    （`day` 写成 `day_index`、`status` 写成 `state`），
    而拼错的表现是"这条被静默丢弃"——测试看着绿，实际什么都没测到。
    """
    return AIMessage(content=json.dumps({"findings": list(findings)}, ensure_ascii=False))


#: 软判据的**默认**回复：什么都不提醒。
#:
#: 为什么要有默认值：绝大多数测试（天气、距离、判据、打回……）跟软判据无关，
#: 它们不该因为"多了一个 LLM 节点"而红，也不该被迫写一份软判据脚本。
#: 空 findings 让 `soft_check` 变成**无副作用的过路节点**。
SOFT_EMPTY = soft_says()


#: 骨架节点的**默认**回复（D77）。大多数测试不关心骨架长什么样，
#: 给一份最小合法骨架让节点安静地过；要测骨架行为时用 `skel_script` 覆盖。
SKEL_EMPTY = ai_text(
    '{"title": "测试骨架", "days": [{"day": 1, "date": null, "theme": "测试", "stops": ["测试点"]}]}'
)


def make_nodes(
    *,
    tool_script: Sequence[AnyMessage] | None = None,
    plan_script: Sequence[AnyMessage] | None = None,
    extract_script: Sequence[AnyMessage] | None = None,
    soft_script: Sequence[AnyMessage] | None = None,
    skel_script: Sequence[AnyMessage] | None = None,
    provider: Any = None,
    today: date | None = None,
    tools: list | None = None,
    **kwargs: Any,
) -> Nodes:
    """造一个全部用假 LLM 的 `Nodes`。

    ⚠️ **工具集与生产同构**（P1/D79 起主图直搜）：`[search_poi, get_weather,
    calc_distance]` —— 生产 `build_runtime` 给什么，这里就是什么。
    """
    active = provider or MockAmapProvider(today=today)
    if tools is None:
        tools = build_amap_tools(active, default_city=MOCK_CITY)
    return Nodes(
        provider=active,
        tools=tools,
        llm_tool=ScriptedChatModel(script=list(tool_script or [])),
        llm_plan=ScriptedChatModel(script=list(plan_script or [])),
        llm_extract=ScriptedChatModel(script=list(extract_script or [])),
        llm_soft=ScriptedChatModel(script=list(soft_script or [SOFT_EMPTY])),
        llm_skel=ScriptedChatModel(script=list(skel_script or [SKEL_EMPTY])),
        today=today,
        **kwargs,
    )


def make_state(**overrides: Any) -> dict[str, Any]:
    """一份"字段齐全但都为空"的 state。

    ⚠️ 字段必须与 `initial_state()` 对齐 —— 少一个字段在 `total=False` 下
    **不会报错**，只会让对应的守卫静默失效（比如漏了 `tool_call_count`，
    上限就永远不触发）。所以这里显式列全。
    """
    base: dict[str, Any] = {
        "session_id": "s1",
        "user_message": "去成都玩三天",
        "messages": [],
        "collected_pois": {},
        "collected_weather": {},
        "tool_call_count": 0,
        "agent_rounds": 0,
        "check_rounds": 0,
        "searched_keywords": [],
        "pending_messages": [],
        "requirements": {},
        "missing_required": [],
        "blocking": [],
    }
    base.update(overrides)
    return base


def pool_of(n: int) -> dict[str, dict[str, Any]]:
    """mock 池里前 n 个 POI 的 state 快照形态。"""
    return {p.poi_id: p.model_dump(mode="json") for p in MOCK_POI_POOL[:n]}


POI_IDS: list[str] = [p.poi_id for p in MOCK_POI_POOL]


__all__ = [
    "POI_IDS",
    "SOFT_EMPTY",
    "ScriptedChatModel",
    "ai_multi_tool_calls",
    "ai_text",
    "ai_tool_call",
    "make_nodes",
    "make_state",
    "pool_of",
    "run",
    "soft_says",
]
