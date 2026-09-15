"""LLM 接入层。**全项目只有这里 `new` 模型客户端。**

═══════════════════════════════════════════════════════════════
 为什么分成「工具用」和「规划用」两个工厂
═══════════════════════════════════════════════════════════════

这不是风格问题，是**被实测逼出来的**（D8）：

| 节点类型 | 思考模式 | 原因 |
|---|---|---|
| **带 tools 的**（`agent_step`） | 🔴 **必须 `disabled`** | ① **成本**：`reasoning_tokens` **计入** `completion_tokens`（实测一个最简请求 = `prompt 42 + completion 163`，推理占 160），工具循环 ≤8 轮 = 多烧数倍。② **确定性**：思考模式下 `temperature` 失效，关掉后 `temperature=0` 才生效 → 工具选择可复现 |
| **不挂 tools 的**（`generate_plan`） | ✅ 可以 `enabled` | 规划真的需要 CoT，而且它**不挂 tools**，不受工具循环那套成本约束 |

**架构本来就要求把这两类节点分开**（D3：循环只收事实，出 JSON 另起固定节点），
所以这个约束**不需要额外设计** —— 分层恰好就是分界线。

⚠️ **一个被推翻的直觉**：思考模式下 **`temperature` 失效**（官方原话 "will also have no effect"）。
所以"压低 temperature 降方差"这条路走不通 —— 别在开思考的节点上调它，调了也不报错，只是没用。

⚠️ **另一个要澄清的**：旧版说"带 tools 开思考会稳定 400"，**2026-09-15 晚复测未复现**
（五种组合全过）。所以上面那条 `disabled` **不是为了躲 400** —— 理由只有成本与确定性。
反复测的脚本在 `backend/scripts/probe_llm_structured.py`。
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from app.config import settings

DEFAULT_TIMEOUT = 120.0
"""单次调用超时（秒）。行程生成实测可能到 30s~2min，给足；但**不能不给** ——
没有超时的 HTTP 客户端在网络半死不活时会挂到天荒地老，而用户以为程序卡了。"""


def _build(model: str, thinking: str, *, temperature: float | None) -> ChatOpenAI:
    kwargs: dict[str, object] = {
        "model": model,
        "api_key": settings.llm_api_key,
        "base_url": settings.llm_base_url,
        "max_retries": 3,  # D26：只重试 429/5xx/超时（SDK 内部就是这么判的）
        "timeout": DEFAULT_TIMEOUT,
        "extra_body": {"thinking": {"type": thinking}},
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    return ChatOpenAI(**kwargs)  # type: ignore[arg-type]


def build_tool_llm() -> ChatOpenAI:
    """给 `agent_step` 用：**关思考**，temperature=0。

    - 关思考：硬约束（见模块 docstring）
    - `temperature=0`：工具选择是**决策**不是创作，要的就是稳定复现
      （思考关掉了，temperature 才真正生效 —— 这也是关思考的附带好处）
    """
    return _build(
        settings.llm_model_tool,
        settings.llm_thinking_tool,
        temperature=0,
    )


def build_plan_llm() -> ChatOpenAI:
    """给 `generate_plan` 用：**开思考**，不设 temperature。

    `temperature=None` 是**故意的**，不是漏写：
    思考模式下它会被服务端忽略（官方明说），设一个值只会让人以为它起作用了。
    留空 = 让代码诚实反映这个事实。
    """
    return _build(
        settings.llm_model_plan,
        settings.llm_thinking_plan,
        temperature=None,
    )


def build_extract_llm() -> ChatOpenAI:
    """给 `parse_intent` 用：**不挂 tools，但依然关思考**，temperature=0。

    这是一个**第三种组合**，所以 D8 那张"带不带 tools"的两行表不够用了。
    真正的分界线不是"带不带 tools"，而是**这个节点在做什么事**：

    | 节点 | 在做什么 | 思考 |
    |---|---|---|
    | `agent_step` | 选工具（决策） | 关（成本 + 可复现） |
    | `generate_plan` | 排行程（创作，需要 CoT） | 开 |
    | `parse_intent` | **从一句话里抠字段**（抽取） | **关** |

    抽取关思考的两个理由：

    1. **要可复现**。"下周三"换算成哪一天，不该每次跑出不同答案 ——
       而思考模式下 `temperature` 失效，等于放弃对它的控制
    2. **不需要 CoT**。抽字段是"文本里有什么就取什么"，
       推理链在这里不产生任何被用到的东西，纯烧 token

    代价：模型算相对日期（"国庆"、"月底"）时会比开思考更容易出错。
    所以 `parse_intent` 的 prompt 里**显式给了今天日期和星期当锚点**，
    并且在代码里校验"日期不能早于今天" —— 用外部约束补上推理的缺口。
    """
    return _build(
        settings.llm_model_tool,
        "disabled",
        temperature=0,
    )


__all__ = [
    "DEFAULT_TIMEOUT",
    "build_extract_llm",
    "build_plan_llm",
    "build_tool_llm",
]
