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


__all__ = ["DEFAULT_TIMEOUT", "build_plan_llm", "build_tool_llm"]
