"""图的 8 个节点。**三层循环的执行体**（结构见 `graph.py`）。

═══════════════════════════════════════════════════════════════
 三层各自归谁管
═══════════════════════════════════════════════════════════════

```
L1  会话层   ① 图外面（M6 的 FastAPI 每次 invoke）—— 本文件不涉及
    └─ L2  工具层   ② agent_step ⇄ tool_step
           ↓ 模型判定事实已够
           ③ generate_plan
           ↓
           L3  校验层   ④ check_plan ─ 不通过 → ⑤ repair → 回 ②
                              └ 通过 / 超限降级 ↓
                              ⑥ soft_check ─ 软判据，**只提醒不打回**
                                    ↓
                              ⑦ render
```

另外两个节点 ⑧ `parse_intent` / ⑨ `ask_more` 在 L2 之前，负责"能不能开工"。

═══════════════════════════════════════════════════════════════
 三个必须在这里兑现的实测结论
═══════════════════════════════════════════════════════════════

**① `tool_step` 必须逐个回应**每一条 `tool_calls`。

DeepSeek 实测：只要有一条 `tool_call` 没被 `ToolMessage` 回应，下一次请求直接 400
（`An assistant message with 'tool_calls' must be followed by tool messages
responding to each 'tool_call_id'`）。
所以这里**没有 `calls[0]`** —— 循环处理全部，并且**异常也要变成 ToolMessage**，
不能让它抛出去（抛出去 = 那条调用永远没有回应 = 下一轮必 400）。

**② 模型调用失败不抛异常，写进 `state["error"]`。**

节点里 `raise` 会让图中止，而已流出的 SSE token 变成孤儿（前端看到半句话然后断流）。
写进 state 则图还能走完 `render`，前端能收到一个完整的失败收尾。

**③ 天气由代码查，不靠模型"想起来调工具"。**

`get_weather` 工具是给**模型看**的（它据此决定室内/室外、要不要提醒带伞）。
但校验层要用的天气，`check_plan` **自己再查一遍** —— 因为硬判据不能建立
在"模型是否碰巧调了某个工具"上。工具是给模型的建议路径，不是代码的事实来源。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from app.graph.draft import (
    DRAFT_SCHEMA_HINT,
    PlanDraft,
    assemble_trip,
    parse_draft,
)
from app.graph.intent import (
    BLOCKING_FIELDS,
    INTENT_SYSTEM_PROMPT,
    Requirements,
    build_ask_text,
    build_intent_prompt,
    merge_requirements,
    parse_intent_json,
)
from app.graph.soft import run_soft_checks
from app.graph.validate import validate_trip
from app.providers.base import AmapProvider
from app.schemas import (
    AmapPoi,
    CheckLevel,
    Trip,
    Validation,
    ValidationIssue,
    Weather,
)
from app.tools.poi_pool import PoiPool, poi_pool_scope

# ══════════════════════════════════════════════════════════════
#  D25 的三个上限
# ══════════════════════════════════════════════════════════════

MAX_TOOL_CALLS = 20
"""单次行程的**工具调用总数**上限。防的是"一轮里发一堆调用"的偶发爆量。"""

MAX_AGENT_ROUNDS = 8
"""`agent_step` 的**执行轮数**上限（D25 的 "L2 = 8"）。防的是"每轮只调一个"的反复空转。

⚠️ 两个上限都在守，不能只留一个 —— 理由见 `state.py` 里 `agent_rounds` 的注释。
"""

MAX_CHECK_ROUNDS = 2
"""L3 打回上限（D25）。超了**降级输出** + 把剩余风险如实列出来，**不继续烧钱重排**。"""

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

FORCE_STOP_TEXT = (
    "（本轮查询次数已达上限，停止继续查询，直接根据现有信息生成行程。）"
)

TOOL_BUDGET_TEXT = (
    "（本次行程的工具调用总次数已达上限，**这个调用没有执行**。\n"
    "请不要再发起任何工具调用，直接根据已经拿到的信息整理结论。）"
)
"""超限调用的回应文案。

⚠️ **必须仍然回一条 `ToolMessage`**，哪怕什么都没做 ——
漏回应的下一次请求会直接 400（见模块 docstring）。
所以"拒绝执行"和"不回应"是两件事，只有前者是安全的。
"""

LLM_FAIL_TEXT = (
    "（AI 服务暂时不可用，本次未能完成规划。请稍后重试 —— 你刚才输入的内容还在。）"
)


# ══════════════════════════════════════════════════════════════
#  Prompt
# ══════════════════════════════════════════════════════════════

AGENT_SYSTEM_PROMPT = """你是行程规划助手。**你现在的工作是收集事实，不是写行程。**

你有三个工具：搜索地点、查天气、计算两地驾车距离。

工作方式：

1. 按用户的偏好搜候选地点。**每个偏好搜 1~2 个关键词就够** ——
   比如"历史古迹"搜一次、"当地小吃"搜一次，**不要反复搜相似的关键词**。
   总共 6~10 次调用已经足够，超过就是重复劳动。
2. 需要判断顺序是否合理时，用算距离工具查相邻两站的车程。
   **不需要为了每一对站点都算一遍**，只算你打算真正相邻的那几对。
3. 如果行程日期在天气可查范围内，查一下天气（一天一次就够）。
4. **信息够了就直接回复一句话说明你打算怎么安排，不要再调用工具。**

硬规则（违反会导致整份行程作废）：

- **所有地点都必须来自 search_poi 的返回**，必须用返回里的 id。
  绝对不要凭记忆或常识写任何地点名或 id。
- 搜不到就换关键词。换两次还是搜不到，就**少安排一个站点**，
  **不要编一个出来** —— 少一站是"信息不足"，编一站是"数据造假"。
- 算距离时只用已经搜出来的 id，不要自己估距离或时间。
- **不要输出 JSON，不要写完整行程表。** 你只负责收集和判断，
  最终结构由后面的节点生成。
"""

PLAN_SYSTEM_PROMPT = """你负责把已经收集到的候选地点排成行程骨架。

硬规则：

1. **只能从用户消息里「候选地点」清单中选**，并且必须用清单里的 id。
   清单外的地点一个都不许出现 —— 哪怕你确信那个地方存在。
2. 天数、日期、同行人、偏好是必须遵守的约束。
3. 你**只负责判断**：去哪几站、什么顺序、每站待多久、为什么适合这个人。
   **不要输出坐标、距离、评分、花费、汇总数字** —— 那些由代码计算，
   你写了也不会被采用，只会让输出不合格。
4. 同一天里站点的 `arrive` 时刻要**自然递进**，不要把三站都排在 09:00。
5. 只输出 JSON 对象，**不要 markdown 代码块**，不要任何解释文字。"""

PLAN_JSON_REMINDER = """输出格式（严格遵守，字段名一个都不能错）：

{schema}

再次强调：`poi_id` 只能取「候选地点」清单里的第一列。"""


# ══════════════════════════════════════════════════════════════
#  小工具
# ══════════════════════════════════════════════════════════════


def _text_of(message: AnyMessage) -> str:
    """把模型返回的 `content` 取成字符串。

    ⚠️ `content` 不一定是 `str` —— 多模态返回是 `list[dict]`。
    直接 `.strip()` 会在那种情况下抛 `AttributeError`，
    而它只在"模型返回了非文本内容"时才出现，很难复现。
    """
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "".join(parts)
    return str(content)


def _pool_from_state(state: dict[str, Any]) -> PoiPool:
    """从 state 的快照重建 POI 池。

    ── 为什么要重建，而不是直接用 `contextvars` 里那个 ──

    `poi_pool` 用的是 `contextvars`，它**不跨 LangGraph 节点保证存活**
    （节点可能在不同的 asyncio 任务里跑），也**不进 checkpoint**。

    所以约定：**池子的真身在 state 的 `collected_pois` 快照里**，
    `contextvars` 只是"工具执行期间的临时投递通道"。
    每个用到池子的节点开头重建一次 —— 多几行代码，换来的是
    "换多少个 asyncio 任务、断线恢复多少次，行为都一样"。

    单条 dump 解析失败时**跳过而不是抛出**：快照是历史数据，
    它坏了最多是少一个候选，不该让整次请求崩掉。
    """
    pool = PoiPool()
    for dump in (state.get("collected_pois") or {}).values():
        try:
            pool.record([AmapPoi.model_validate(dump)])
        except ValidationError:
            continue
    return pool


def _pool_dump(pool: PoiPool) -> dict[str, dict[str, Any]]:
    """池子 → 可进 checkpoint 的快照。"""
    return {poi.poi_id: poi.model_dump(mode="json") for poi in pool.all()}


def _describe_pool(pool: PoiPool) -> str:
    """把候选池渲染成给模型看的清单。

    ⚠️ **清单就是模型的"封闭世界"**。它会从这里挑 id，
    而不是从记忆里挑 —— 这把"事后校验"提前成了"事前约束"。
    两个通道（模型看文明细 / 机器留全量字段）的分离见 `amap_tools.py`。
    """
    lines: list[str] = []
    for poi in pool.all():
        parts = [poi.poi_id, poi.name]
        parts.append(f"{poi.lng},{poi.lat}")
        parts.append(f"开放{poi.open_time}" if poi.open_time else "开放时间未知")
        if poi.rating:
            parts.append(f"评分{poi.rating}")
        if poi.cost_per_person is not None:
            parts.append(f"人均{poi.cost_per_person}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _describe_requirements(req: Requirements) -> str:
    """需求 → 给模型看的一段文字。

    ⚠️ 没填的字段写"未提供"而不是省略：**省略会让模型以为那个约束不存在**，
    于是它可能自己编一个（"用户没提预算，那我按人均 800 排"）。
    写"未提供"就是明说"这里没有约束，你可以自由发挥"。
    """
    t = req.travelers
    who = (
        f"成人×{t.adults}"
        + (f"，儿童×{t.children}" if t.children else "")
        + (f"，老人×{t.elders}" if t.elders else "")
        if t
        else "未提供"
    )
    return "\n".join(
        [
            f"目的地：{req.destination or '未提供'}",
            f"出发日：{req.date.isoformat() if req.date else '未提供'}",
            f"天数：{req.days if req.days is not None else '未提供'}",
            f"同行人：{who}",
            f"预算：{'人均 ' + str(req.budget) if req.budget else '未提供'}",
            f"偏好：{'、'.join(req.preferences) if req.preferences else '未提供'}",
            f"特别要求：{req.notes or '未提供'}",
        ]
    )


def build_plan_prompt(req: Requirements, pool: PoiPool, hint: str | None = None) -> str:
    """拼生成节点用的 user prompt。"""
    blocks = [
        "# 用户需求",
        _describe_requirements(req),
        "",
        f"# 候选地点（{len(pool)} 个，**只能从这里面选**）",
        _describe_pool(pool),
    ]
    if hint:
        blocks += ["", "# 上一版被打回的原因（这一版必须避开）", hint]
    blocks += ["", "# 输出", PLAN_JSON_REMINDER.format(schema=DRAFT_SCHEMA_HINT)]
    return "\n".join(blocks)


# ══════════════════════════════════════════════════════════════
#  节点集合
# ══════════════════════════════════════════════════════════════


@dataclass
class Nodes:
    """9 个节点 + 它们共用的依赖。

    做成一个类而不是 9 个模块级函数，是为了让**依赖可见**：
    图要跑起来必须凑齐 provider / 四个 LLM / 三个工具，
    这些在 `graph.py` 里一次性组装，节点函数本身不带全局状态，
    测试里可以整体替换（塞 mock provider、塞假 LLM）。
    """

    provider: AmapProvider
    tools: list[BaseTool]
    llm_tool: BaseChatModel
    llm_plan: BaseChatModel
    llm_extract: BaseChatModel
    llm_soft: BaseChatModel
    """给 `soft_check`（软判据，D45）。**它是唯一一个"锦上添花"的 LLM 调用** ——
    所以要能整个换掉（测试里塞假 LLM 或塞一个必抛异常的假 LLM，
    验证"软判据挂了不影响行程产出"）。"""

    today: Date | None = None
    """测试注入固定"今天"。**生产代码不要传** —— 它存在的唯一理由是
    让"相对日期换算"和"日期不能是过去"这两条可被断言。"""

    max_tool_calls: int = MAX_TOOL_CALLS
    max_agent_rounds: int = MAX_AGENT_ROUNDS
    max_check_rounds: int = MAX_CHECK_ROUNDS

    _tools_by_name: dict[str, BaseTool] = field(default_factory=dict, init=False, repr=False)
    _agent_llm: BaseChatModel = field(default=None, init=False, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._tools_by_name = {t.name: t for t in self.tools}
        # bind_tools 每次调用都重算一遍 schema，缓存下来
        self._agent_llm = (
            self.llm_tool.bind_tools(self.tools) if self.tools else self.llm_tool
        )

    def _now_date(self) -> Date:
        return self.today or datetime.now().date()

    # ══════════════════════════════════════════════════════════
    #  ⑦ parse_intent —— 需求抽取
    # ══════════════════════════════════════════════════════════

    async def parse_intent(self, state: dict[str, Any]) -> dict[str, Any]:
        """把用户这一轮的话抽成「必问 7 项」，并与已有需求合并。

        **抽取失败不猜**：拿不到就按"什么都没说"处理，让 `ask_more` 去问。
        顺手编一个目的地（比如从上下文里随便挑个城市）比直接问一遍危险得多 ——
        用户会以为 agent 记住了，实际记住的是幻觉。
        """
        message = (state.get("user_message") or "").strip()
        existing = state.get("requirements") or {}
        if not message:
            return {
                "missing_required": list(BLOCKING_FIELDS),
                "requirements": existing,
                "error": "收到空消息",
            }

        today = self._now_date()
        prompt = build_intent_prompt(
            message,
            today=today,
            weekday=_WEEKDAYS[today.weekday()],
            existing=existing,
        )

        try:
            raw = await self.llm_extract.bind(response_format={"type": "json_object"}).ainvoke(
                [SystemMessage(content=INTENT_SYSTEM_PROMPT), HumanMessage(content=prompt)]
            )
        except Exception as exc:  # noqa: BLE001 —— 见模块 docstring 的 ②
            return {
                "requirements": existing,
                "missing_required": list(BLOCKING_FIELDS),
                "error": f"需求抽取调用失败：{type(exc).__name__}: {exc}",
            }

        parsed, err = parse_intent_json(_text_of(raw))
        if parsed is None:
            return {
                "requirements": existing,
                "missing_required": list(BLOCKING_FIELDS),
                "error": f"需求抽取结果无法解析：{err}",
            }

        merged = merge_requirements(Requirements.model_validate(existing), parsed)

        # ── 日期合理性：抽错了不如不要 ──
        # 抽取节点关了思考（见 `llm.py`），模型算相对日期会偏。
        # **过去的日期一定错了**，而且下游没有任何环节能发现 ——
        # 一个合法的日期字符串看起来完全正常，天气只会返回"窗口外无法判定"。
        if merged.date is not None and merged.date < today:
            dropped = merged.model_copy(update={"date": None})
            return {
                "requirements": dropped.model_dump(mode="json"),
                # ⚠️ 用 `dropped.missing_blocking()` 而不是常量 `BLOCKING_FIELDS` ——
                # 丢了日期不等于丢了目的地。写常量会让"明明说了去哪座城市"
                # 的用户被重新追问一遍目的地。
                "missing_required": dropped.missing_blocking(),
                "error": (
                    f"抽取出的日期 {merged.date.isoformat()} 早于今天 "
                    f"{today.isoformat()}，已丢弃并重新追问"
                ),
            }

        return {
            "requirements": merged.model_dump(mode="json"),
            "missing_required": merged.missing_blocking(),
        }

    # ══════════════════════════════════════════════════════════
    #  ⑧ ask_more —— 追问（终点之一）
    # ══════════════════════════════════════════════════════════

    async def ask_more(self, state: dict[str, Any]) -> dict[str, Any]:
        """缺阻塞项时问用户。**模板化措辞**，不调模型（理由见 `intent.build_ask_text`）。"""
        req = Requirements.model_validate(state.get("requirements") or {})
        missing = list(state.get("missing_required") or req.missing_blocking())
        text = build_ask_text(req, missing)
        return {"ask": text, "messages": [AIMessage(content=text)]}

    # ══════════════════════════════════════════════════════════
    #  ② agent_step —— L2 里的模型侧
    # ══════════════════════════════════════════════════════════

    async def agent_step(self, state: dict[str, Any]) -> dict[str, Any]:
        """调模型，让它决定下一步做什么（继续调工具 / 收工）。

        这一跳同时兑现两件事：**steering 的落地** 和 **D25 的 L2 守卫**。
        """
        update: dict[str, Any] = {}
        fresh: list[AnyMessage] = []

        # ── ① steering：把用户插话吃进上下文 ──
        # 放在这里（而不是单独一个节点）是因为**要在这里生效**：
        # 下一句就要交给模型了，插话必须在它之前进上下文。
        pending = list(state.get("pending_messages") or [])
        if pending:
            fresh += [
                HumanMessage(content=f"【用户在你工作期间插话，请优先处理】{p}") for p in pending
            ]
            update["pending_messages"] = []  # 覆盖语义，清空（`state.py` 里刻意不加 reducer）

        # ── ② 守卫：两个上限任一触发就强制收尾 ──
        used_calls = state.get("tool_call_count") or 0
        used_rounds = state.get("agent_rounds") or 0
        if used_calls >= self.max_tool_calls or used_rounds >= self.max_agent_rounds:
            update["messages"] = [*fresh, AIMessage(content=FORCE_STOP_TEXT)]
            return update

        # ── ③ 调模型 ──
        history = list(state.get("messages") or [])
        try:
            ai = await self._agent_llm.ainvoke(
                [SystemMessage(content=AGENT_SYSTEM_PROMPT), *history, *fresh]
            )
        except Exception as exc:  # noqa: BLE001 —— 见模块 docstring 的 ②
            update["messages"] = [*fresh, AIMessage(content=LLM_FAIL_TEXT)]
            update["error"] = f"工具循环模型调用失败：{type(exc).__name__}: {exc}"
            return update

        update["messages"] = [*fresh, ai]
        update["agent_rounds"] = 1  # reducer 是 add，累加到 state
        return update

    # ══════════════════════════════════════════════════════════
    #  ③ tool_step —— L2 里的工具侧
    # ══════════════════════════════════════════════════════════

    async def tool_step(self, state: dict[str, Any]) -> dict[str, Any]:
        """执行上一条 AI 消息里的**全部** `tool_calls`。

        🔴 **逐个回应，一条都不能漏。**漏一条 → 用户看到的是一次莫名其妙的 400
        （实测结论，见模块 docstring ①）。所以这里：
        · 用 `for` 而不是 `calls[0]`
        · 工具不存在 → 也回一条 `ToolMessage` 说明情况
        · 工具抛异常 → 也回一条 `ToolMessage` 说明情况

        池子在**本节点内部**开 `contextvars` 作用域：
        工具往里写、写完立刻取快照存进 state。**不依赖它跨节点存活**（见 `_pool_from_state`）。

        另外这里还有**第二道预算闸**（执行预算），理由见函数中部的注释 ——
        实测过：只有 `agent_step` 那一处守卫时，总数会超到 23。
        """
        history = list(state.get("messages") or [])
        if not history:
            return {}
        calls = list(getattr(history[-1], "tool_calls", None) or [])
        if not calls:
            return {}

        pool = _pool_from_state(state)
        results: list[ToolMessage] = []
        errors: list[str] = []

        # ── 执行预算 ──
        # 🔴 **2026-09-16 实测补上的缺口**：第一次跑真实模型拿到了
        # `工具调用 23 次`，**超过了 D25 定的 20**。
        #
        # 原因是守卫只能"在下一轮开始前"判断，而模型**一轮能发多个调用**：
        # 已经有 19 次时 `agent_step` 依然会放行（19 < 20），
        # 模型这一轮又发 4 个 → 总数 23。
        #
        # 所以"总上限"必须**在这里**再拦一道：只执行余额内的，
        # 其余的**回一条拒绝消息但不执行**。这样：
        # · 总数真正封顶（而不是"大约 20"）
        # · 仍然每条都回应（漏回应 = 下一轮 400）
        #
        # 这是"两处判据"少数正当的场合之一 —— 它们守的是**不同的失控方式**：
        # `agent_step` 防"继续烧钱问模型"，这里防"单轮调用爆量"。
        used_calls = state.get("tool_call_count") or 0
        allowed = max(0, self.max_tool_calls - used_calls)

        with poi_pool_scope(pool) as active:
            for index, call in enumerate(calls):
                name = str(call.get("name") or "")
                args = call.get("args") or {}
                call_id = str(call.get("id") or "")

                if index >= allowed:
                    results.append(
                        ToolMessage(
                            content=TOOL_BUDGET_TEXT, tool_call_id=call_id, name=name or "unknown"
                        )
                    )
                    continue

                tool = self._tools_by_name.get(name)

                if tool is None:
                    # 模型编了一个不存在的工具 —— 回话让它改，**不能跳过**
                    results.append(
                        ToolMessage(
                            content=(
                                f"没有名为「{name}」的工具。可用工具："
                                f"{'、'.join(self._tools_by_name) or '（一个都没有）'}"
                            ),
                            tool_call_id=call_id,
                            name=name or "unknown",
                        )
                    )
                    continue

                try:
                    raw = await tool.ainvoke(args)
                    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                except Exception as exc:  # noqa: BLE001 —— 异常也必须变成回应
                    text = (
                        f"工具「{name}」执行失败：{type(exc).__name__}: {exc}\n"
                        f"可以先换个关键词或换个工具试试，不要因此就凭记忆编数据。"
                    )
                    errors.append(f"{name}: {exc}")

                results.append(ToolMessage(content=text, tool_call_id=call_id, name=name))

            snapshot = _pool_dump(active)

        update: dict[str, Any] = {
            "messages": results,
            "tool_call_count": len(calls),
            "collected_pois": snapshot,
        }
        if errors:
            update["error"] = "工具执行出错：" + "；".join(errors)
        return update

    # ══════════════════════════════════════════════════════════
    #  ④ generate_plan —— 出草稿（D3：循环只收事实，出 JSON 在这里）
    # ══════════════════════════════════════════════════════════

    async def generate_plan(self, state: dict[str, Any]) -> dict[str, Any]:
        """在**封闭世界**里出草稿。

        🔴 **候选池为空就直接失败，不调模型。**
        池子空 = 这一轮模型一次 `search_poi` 都没调（或全搜不到）。
        这时让模型"生成行程"，它必然从记忆里编 —— 而且会成功，
        因为生成节点看不到池子是空的这件事的严重后果。**这里必须拦住。**

        解析失败**重试一次**并把 Pydantic 报错回灌给模型：
        `json_object` 模式只保证"是合法 JSON"，不保证字段对，
        而字段错是模型自己能修的那一类错（跟"编地点"不同）。
        """
        pool = _pool_from_state(state)
        if len(pool) == 0:
            return {
                "error": (
                    "候选池是空的 —— 这一轮模型没有成功调用过 search_poi，"
                    "无法在封闭世界内生成行程。"
                )
            }

        req = Requirements.model_validate(state.get("requirements") or {}).with_defaults()
        hint = state.get("repair_hint") or None

        base: list[AnyMessage] = [
            SystemMessage(content=PLAN_SYSTEM_PROMPT),
            HumanMessage(content=build_plan_prompt(req, pool, hint)),
        ]
        llm = self.llm_plan.bind(response_format={"type": "json_object"})

        last_err: str | None = None
        for _ in range(2):
            messages = list(base)
            if last_err:
                messages.append(
                    HumanMessage(
                        content=(
                            f"你上一次的输出不合格：{last_err}\n"
                            f"请修正后**重新输出完整 JSON**（不要片段，不要解释）。"
                        )
                    )
                )
            try:
                raw = await llm.ainvoke(messages)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"生成节点模型调用失败：{type(exc).__name__}: {exc}"}

            draft, last_err = parse_draft(_text_of(raw))
            if draft is not None:
                return {"draft": draft.model_dump(mode="json")}

        return {"error": f"模型连续两次输出的草稿都不合格：{last_err}"}

    # ══════════════════════════════════════════════════════════
    #  ⑤ check_plan —— L3 校验
    # ══════════════════════════════════════════════════════════

    async def check_plan(self, state: dict[str, Any]) -> dict[str, Any]:
        """补全 + 校验，产出 `Trip`。

        **两类硬错在这里合流**（M3）：

        | 来源 | 拦的是什么 | 谁产出 |
        |---|---|---|
        | 组装阶段 | `poi_id` 不在候选池里 → 那些站**根本进不了 `Trip`** | `assemble_trip` 的 `assemble_blocking` |
        | 校验阶段 | 5 条判据（存在性 / 营业时间 / 车程 / 天气 / 走路量） | `validate.validate_trip` 的 `report.blocking` |

        两者拼成 `blocking` 交给条件边 —— 有就 `repair` 打回（上限 2 轮），
        没有就 `render` 收尾。

        ⚠️ **只有 `fail` 会进来。`unknown` 一条都不许进来**（见下方 `validation` 的注释），
           否则 agent 会去打回重排一个**数据层面、它永远修不好**的问题 ——
           白烧两轮，最后还是降级输出。
        """
        draft_dump = state.get("draft") or {}
        if not draft_dump:
            # ⚠️ **不要覆盖上游的错误。**没有草稿最常见的原因是 `generate_plan`
            # 已经失败了（池子空、模型连续两次输出不合格）。这时再写一句
            # "没有草稿可校验"，用户看到的就是最后一步的症状而不是根因 ——
            # 而根因是唯一能指导他"该重试还是该换个说法"的信息。
            if state.get("error"):
                return {}
            return {"error": "没有草稿可校验"}
        try:
            draft = PlanDraft.model_validate(draft_dump)
        except ValidationError as exc:
            return {"error": f"草稿结构不合法：{exc.errors()[:3]}"}

        req = Requirements.model_validate(state.get("requirements") or {}).with_defaults()
        destination = req.destination or "未知目的地"
        pool = _pool_from_state(state)
        rounds_used = state.get("check_rounds") or 0

        # ── 天气：代码自己查（见模块 docstring ③）──
        weather_by_date: dict[str, Weather] = {}
        for draft_day in draft.days:
            try:
                weather_by_date[draft_day.date.isoformat()] = await self.provider.get_weather(
                    destination, draft_day.date
                )
            except Exception:  # noqa: BLE001
                # 拿不到就是拿不到 —— `assemble_trip` 会写 unavailable，**不编**
                continue

        trip, assemble_blocking = await assemble_trip(
            draft,
            pool,
            self.provider,
            weather_by_date,
            destination=destination,
            session_id=state.get("session_id"),
            user_id=1,
            rounds=rounds_used,
        )

        # ── M3：整份行程上跑 5 条硬判据 ──
        # `validate_trip` **原地**把结果写进 `trip.days[].checks` / `stops[].checks`
        # （每次先清空，所以重算是幂等的 —— 不清的话"重算"会让每个判据出现两遍）。
        # 传 `distance_fn` 是为了让它能算**跨天段**（昨天最后一站 → 今天第一站）：
        # 那一段是"当天第 1 站"唯一能拿到的距离，`from_prev_km` 对它恒为 0。
        report = await validate_trip(
            trip, pool, req, distance_fn=self.provider.calc_distance
        )

        # 两类硬错合起来：
        # ① 组装阶段拦下的（`poi_id` 不在池子里 —— 那些站根本没能进 `Trip`）
        # ② 校验阶段发现的（车程 / 开放时间 / 天气 / 走路量）
        blocking = [*assemble_blocking, *report.blocking]
        trip.summary.hard_errors = len(blocking)

        remaining = [
            ValidationIssue(code="poi_exists", msg=msg, level=CheckLevel.HARD)
            for msg in assemble_blocking
        ] + [
            ValidationIssue(code=check.code, msg=check.msg or "", level=CheckLevel.HARD)
            for check in report.hard_failed
        ]
        # ⚠️ `report.unknown`（判不了的那些）**故意不进 `remaining`**：
        #    `ValidationIssue.level` 只有 hard / soft 两个值，而 `unknown` 两者都不是 ——
        #    硬塞进去会让"前端把它当成一种风险"变成既成事实。
        #    它只留在 `checks` 里显示灰色，**不打回也不算通过**。
        validation = Validation(rounds=rounds_used, fixed=[], remaining=remaining)
        trip.validation = validation

        return {
            "trip": trip.model_dump(mode="json"),
            "validation": validation.model_dump(mode="json"),
            "blocking": blocking,
            "collected_weather": {
                k: v.model_dump(mode="json") for k, v in weather_by_date.items()
            },
            # ⚠️ **这里不累加 `check_rounds`。** 它记的是"已经重排过几轮"，
            # 而"这轮发现了错误"和"重排了一轮"是两件事 —— 累加该发生在
            # `repair` 真正把活派回去的时候。由 `repair` 累加的实际效果：
            # 上限 2 时 plan 会跑 3 次（初次 + 2 次重排），这才是 D25 的本意。
        }

    # ══════════════════════════════════════════════════════════
    #  ⑥ soft_check —— 软判据（D45）
    # ══════════════════════════════════════════════════════════

    async def soft_check(self, state: dict[str, Any]) -> dict[str, Any]:
        """跑 4 条软判据（LLM 判），**只写提醒，从不打回**。

        为什么是**独立节点、而且在 `check_plan` 之后**：

        | 放法 | 后果 |
        |---|---|
        | 塞进 `check_plan` | `check_plan` 每轮都跑 → 打回 2 轮就是 3 次 LLM 调用，而软判据 **fail 又不会让 `blocking` 多一条** → 那两次是纯浪费 |
        | ✅ 独立节点，挂在 `check_plan` 与 `render` 之间 | 只在**行程定稿前**跑一次（无论正常收尾还是打回超限降级，都会经过它） |

        放错地方的代价不是"慢一点"，是**它永远修不好任何东西**：
        软判据不打回，所以进打回循环对它毫无意义。

        ⚠️ **失败不写 `state["error"]`**（与 `generate_plan` 的纪律相反，见模块 docstring ②）：
        那条纪律的出发点是"用户不能什么都拿不到"。而这里如果写 error，
        就把一份**本来完全可用的行程**废掉了 —— 为了 4 句可有可无的提醒，
        代价完全不对等。所以这里只记日志字段，行程照常输出。
        """
        trip_dump = state.get("trip")
        if not trip_dump:
            return {}
        try:
            trip = Trip.model_validate(trip_dump)
        except ValidationError:
            # 上游已经出过错了（`render` 会再报一次），这里不抢着报
            return {}

        req = Requirements.model_validate(state.get("requirements") or {}).with_defaults()
        result = await run_soft_checks(trip, req, self.llm_soft)

        return {
            "trip": trip.model_dump(mode="json"),
            # 丢弃清单留在 state 里给 CLI / 评测看 —— 它是"防幻觉闸门实际拦了多少"的唯一观测点
            "soft_report": {
                "applied": result.applied,
                "warnings": result.warnings,
                "unknown": result.unknown,
                "dropped": result.dropped,
                "error": result.error,
            },
        }

    # ══════════════════════════════════════════════════════════
    #  ⑦ repair —— 打回
    # ══════════════════════════════════════════════════════════

    async def repair(self, state: dict[str, Any]) -> dict[str, Any]:
        """把硬错写回对话，让模型带着错误信息**重新走一遍 L2**。

        **`check_rounds` 在这里累加**（不是在 `check_plan`）：它记的是
        "已经重排过几轮"，而重排是在这一步真正发生的。
        放错地方的后果见 `graph.py` 的 `after_check`。

        ⚠️ **保留全部历史消息**：模型需要看到"我之前搜到了什么、为什么这个 id 错了"，
        把它清空等于让它从零重来，会重复搜同样的东西。
        """
        blocking = list(state.get("blocking") or [])
        lines = ["上一版行程里有**不是工具搜出来**的地点，必须改掉："]
        lines += [f"- {msg}" for msg in blocking]
        lines += [
            "",
            "请重新安排一次。需要的地点：要么先 `search_poi` 搜出来、用它的 id；",
            "要么就少安排一站。**不要凭记忆填任何地点。**",
            "前面工具已经返回过的 id 可以直接复用，不用重复搜。",
        ]
        return {
            "messages": [HumanMessage(content="\n".join(lines))],
            "blocking": [],
            "repair_hint": "\n".join(f"- {m}" for m in blocking),
            "check_rounds": 1,  # add reducer → 累加"重排过几轮"
        }

    # ══════════════════════════════════════════════════════════
    #  ⑨ render —— 出口
    # ══════════════════════════════════════════════════════════

    async def render(self, state: dict[str, Any]) -> dict[str, Any]:
        """终点节点。M1 只做一个**序列化往返检查**，M6 在这里发 SSE 的 `trip` 事件。

        往返检查是 `state.py` 那条设计纪律的兑现：state 里只放
        "dump 得出来又 validate 得回去"的朴素结构。如果这里 validate 失败，
        说明某个节点往 state 里塞了非契约形状的东西 —— 早发现比在
        M5 接 checkpointer 之后发现好得多（那时错误会表现为"恢复后行为不一致"）。
        """
        trip_dump = state.get("trip")
        if not trip_dump:
            return {}
        try:
            Trip.model_validate(trip_dump)
        except ValidationError as exc:
            return {
                "error": (
                    "行程结构不合法（序列化往返失败，说明有节点往 state 里塞了"
                    f"非契约形状的数据）：{exc.errors()[:3]}"
                )
            }
        return {}


__all__ = [
    "AGENT_SYSTEM_PROMPT",
    "FORCE_STOP_TEXT",
    "LLM_FAIL_TEXT",
    "MAX_AGENT_ROUNDS",
    "MAX_CHECK_ROUNDS",
    "MAX_TOOL_CALLS",
    "Nodes",
    "PLAN_SYSTEM_PROMPT",
    "TOOL_BUDGET_TEXT",
    "build_plan_prompt",
]
