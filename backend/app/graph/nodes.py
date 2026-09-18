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

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
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
from app.graph.subagent import (
    drain_subagent_trace,
    keyword_ledger_scope,
    subagent_trace_scope,
)
from app.utils import text_of

logger = logging.getLogger(__name__)

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

搜索地点由**搜索子 agent** 负责：你用 `task` 工具给它派活，它搜完给你一份带 id 的精选清单。
天气和距离由你直接查（工具：查天气、算驾车距离）。

工作方式：

1. 按用户偏好拆成 1~3 个搜索任务，每个任务一次 `task` 调用。
   任务描述必须**自包含**（城市 + 找什么 + 约束），因为子 agent 看不到我们的对话。
   比如一个任务找景点、一个任务找吃的。不要派 4 个以上。
   **相互独立的任务要在同一次回复里一起派**（并行执行，省时间）：
   例如「找景点」和「找餐厅」互不依赖，就一次同时发两个 `task` 调用；
   只有当下一个任务的描述取决于上一个的结果时才分开派。
2. 需要判断顺序是否合理时，用算距离工具查相邻两站的车程。
   **不需要为了每一对站点都算一遍**，只算你打算真正相邻的那几对。
3. 如果行程日期在天气可查范围内，查一下天气（一天一次就够）。
4. **信息够了就直接回复一句话说明你打算怎么安排，不要再调用工具。**

硬规则（违反会导致整份行程作废）：

- **所有地点都必须来自 task 返回的清单**，必须用清单里的 id。
  绝对不要凭记忆或常识写任何地点名或 id。
- 清单不合适就再派一次任务、换个描述。还是找不到就**少安排一个站点**，
  **不要编一个出来** —— 少一站是"信息不足"，编一站是"数据造假"。
- 算距离时只用清单里出现过的 id，不要自己估距离或时间。
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


SKEL_SYSTEM_PROMPT = """你负责给出行程的**快速骨架**：哪天去哪、每天的主题。
这是一个"先看版" —— 用户在正式搜索和验证完成前先看到大致安排，
所以**不要查任何数据，不要调用任何工具，凭用户需求直接排出框架**。

规则：
- 只用用户提到的地点和你对该目的地的高置信度常识（著名地标可以写）。
- 每天 2~4 个地点，按合理的地理顺序排（同一天的地点应该相邻）。
- 用户指定必去的地点（如"必须包含故宫和长城"）必须出现。
- 天数以用户需求为准；推不出每天的具体日期就填 null。
- 这份骨架**没有经过任何验证**，所以它后面一定会有正式行程替换 —— 不追求精确。
"""

SKEL_JSON_REMINDER = """输出格式（严格遵守，只输出 JSON，不要任何解释文字）：
{"title": "北京 5 天行程", "days": [{"day": 1, "date": "2026-10-02 或 null", "theme": "主题短语", "stops": ["地点名", "地点名"]}]}"""


def build_skel_prompt(message: str, requirements: dict[str, Any], today: Date) -> str:
    """骨架节点的用户侧 prompt：用户原话 + 已抽到的需求 + 今天（算日期用）。"""
    req = Requirements.model_validate(requirements or {})
    party_bits: list[str] = []
    if req.travelers:
        if req.travelers.adults:
            party_bits.append(f"{req.travelers.adults}成人")
        if req.travelers.children:
            party_bits.append(f"{req.travelers.children}小孩")
        if req.travelers.elders:
            party_bits.append(f"{req.travelers.elders}老人")
    lines = [
        "# 用户原话",
        message,
        "",
        "# 已确认的需求",
        f"- 目的地：{req.destination or '（未说）'}",
        f"- 出发日期：{req.date.isoformat() if req.date else '（未说）'}",
        f"- 天数：{req.days or '（未说）'}",
        f"- 同行人：{'、'.join(party_bits) or '（未说）'}",
        f"- 预算：{req.budget if req.budget is not None else '（未说）'}",
        f"- 偏好：{'、'.join(req.preferences) or '（未说）'}",
        f"- 特别要求：{req.notes or '（未说）'}",
        "",
        "# 今天",
        today.isoformat(),
        "",
        "# 输出",
        SKEL_JSON_REMINDER,
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
#  小工具
# ══════════════════════════════════════════════════════════════


def _no_coverage_error(provider_name: str, destination: str | None) -> str:
    """数据源覆盖不到目的地时的**人话**报错（D67）。

    ⚠️ 这段话是给**用户**看的，所以必须回答三件事：哪座城市、为什么不行、怎么办。

    旧文案是"这一轮模型没有成功调用过 search_poi" —— 它把
    「调了 9 次、每次数据源都返回空」也说成「模型没调过」，
    **作者本人被它误导过**（第一反应是"我不是该调高德吗"）。
    一次误导的成本就高于改这段话的成本。
    """
    where = destination or "这个目的地"
    if provider_name == "mock":
        return (
            f"当前用的是内置演示数据，它只有成都的地点和天气，覆盖不到「{where}」。"
            f"两条出路：① 目的地换成成都；"
            f"② 配好 AMAP_WEBSERVICE_KEY（高德「Web服务」那把 Key）并重启后端，走真实高德数据。"
        )
    return f"当前数据源（{provider_name}）覆盖不到「{where}」，无法生成行程。"


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
    图要跑起来必须凑齐 provider / 四个 LLM / 三个工具
    （搜索子 agent 在 `build_runtime` 里已被包成 `task` 工具 ——
    对 `Nodes` 来说它只是一个普通工具，子图的存在对节点透明），
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

    llm_skel: BaseChatModel | None = None
    """给 `make_skeleton`（快速骨架，D77）。None 时回退到 `llm_extract`
    （同形：不挂 tools + 关思考 + temp=0）—— 生产由 `build_runtime` 传专用实例，
    测试不传也能跑，只是骨架走抽取档。"""

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
        self.llm_skel = self.llm_skel or self.llm_extract
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
            raw = await self.llm_extract.ainvoke(
                [SystemMessage(content=INTENT_SYSTEM_PROMPT), HumanMessage(content=prompt)]
            )
        except Exception as exc:  # noqa: BLE001 —— 见模块 docstring 的 ②
            return {
                "requirements": existing,
                "missing_required": list(BLOCKING_FIELDS),
                "error": f"需求抽取调用失败：{type(exc).__name__}: {exc}",
            }

        parsed, err = parse_intent_json(text_of(raw))
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
    #  ⑩ make_skeleton —— 快速骨架（D77，"首结果延迟"优化）
    # ══════════════════════════════════════════════════════════

    async def make_skeleton(self, state: dict[str, Any]) -> dict[str, Any]:
        """搜索循环开始前，先用**一次不挂工具的 LLM 调用**（约 20~30s）给出行程框架。

        动机（2026-09-18 实测）：完整规划 9m48s，其中 85% 是 LLM 调用次数 ×
        单次延迟 —— 精排结果最快也要 3 分钟以上才出来。行业共识（调研 2026-09-18）
        是"正确指标是**首结果延迟**，不是完成时间"：先给可用结果，验证后台继续。

        🔴 **这是一个 best-effort 节点**：任何失败（LLM 挂了、JSON 解析不出、
        结构不对）都只返回 `{}`，**绝不写 error、绝不打断主流程** ——
        骨架是锦上添花，它的失败模式必须是"用户没看到骨架"，不是"行程没了"。
        与 `soft_check` 同一条哲学：装饰性调用不许有破坏性失败。
        """
        message = (state.get("user_message") or "").strip()
        if not message:
            return {}
        # 追问轮不排骨架：需求都没齐，骨架只会误导
        if state.get("missing_required"):
            return {}

        prompt = build_skel_prompt(message, state.get("requirements") or {}, self._now_date())
        messages = [SystemMessage(content=SKEL_SYSTEM_PROMPT), HumanMessage(content=prompt)]

        # 解析失败重试一次（与 generate_plan 同策略：JSON 合法性模型自己能修）
        text = ""
        for _attempt in range(2):
            try:
                resp = await self.llm_skel.ainvoke(messages)
                text = resp.content if isinstance(resp.content, str) else str(resp.content)
                text = text.strip()
                if text.startswith("```"):
                    text = "\n".join(
                        ln for ln in text.splitlines() if not ln.strip().startswith("```")
                    ).strip()
                payload = json.loads(text)
                days = []
                for i, d in enumerate(payload.get("days") or [], start=1):
                    stops = [str(s) for s in (d.get("stops") or []) if str(s).strip()]
                    theme = str(d.get("theme") or "").strip()
                    if not theme and not stops:
                        continue
                    days.append(
                        {
                            "day": int(d.get("day") or i),
                            "date": (str(d.get("date")).strip() or None)
                            if d.get("date")
                            else None,
                            "theme": theme,
                            "stops": stops,
                        }
                    )
                if not days:
                    raise ValueError("days 为空")
                title = str(payload.get("title") or "").strip() or "行程骨架（草排）"
                # 天数校验：骨架声称的天数与需求差太多时仍然放行 ——
                # 它本来就不精确，正式行程会替换它；这里不设硬闸。
                return {"skeleton": {"title": title, "days": days}}
            except Exception as exc:  # noqa: BLE001
                logger.warning("make_skeleton 第 %s 次解析失败：%s", _attempt + 1, exc)
                if text:
                    messages.append(AIMessage(content=text))
                messages.append(
                    HumanMessage(content="上一次输出无法解析，请重新只输出符合格式的 JSON。")
                )
        return {}

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

        # ── ②.5 守卫：数据源覆盖不到，就别进工具循环（D67-B）──
        # 与上面那道守卫**同构**，而且必须在同一层：`after_agent` 只看
        # "最后一条 AI 消息有没有 tool_calls"，这里回一条**纯文本**，
        # 条件边自然导向 `generate_plan`（那里会把同一个原因再报一次）。
        # 拦在这里的理由：进了循环就会派子代理反复空搜 —— 实测非成都目的地
        # 白等 **159s**（其中 ≈100s 花在 7 次注定为空的搜索）才报错，
        # 而这些时间是 100% 可预知地浪费。
        # 判据由 provider 自述（`covers`）：real 档恒为 True → 切真数据后本守卫**自动失效**。
        destination = (state.get("requirements") or {}).get("destination")
        if not self.provider.covers(destination):
            text = _no_coverage_error(self.provider.name, destination)
            update["messages"] = [*fresh, AIMessage(content=text)]
            update["error"] = text
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

    async def tool_step(self, state: dict[str, Any], config: RunnableConfig | None = None) -> dict[str, Any]:
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

        # trace 作用域与池子作用域同层：task 工具（搜索子 agent）执行期间
        # 往里写过程记录，节点结束时 drain 回 state —— 这就是 M4 验收里
        # "主 agent 的 trace 里能看到它调了子 agent"的落点。
        # D71 的搜索关键词台账。**拷一份进来**、跑完写回 ——
        # 不原地改 state 里那个 list：state 值可能被多跳共享，
        # 原地改等于在别人不知道的情况下改历史，排查时对不上账。
        keywords_ledger = list(state.get("searched_keywords") or [])

        with (
            poi_pool_scope(pool) as active,
            subagent_trace_scope(),
            keyword_ledger_scope(keywords_ledger),
        ):

            async def _run_one(call: dict[str, Any]) -> ToolMessage:
                name = str(call.get("name") or "")
                args = call.get("args") or {}
                call_id = str(call.get("id") or "")
                tool = self._tools_by_name.get(name)

                if tool is None:
                    # 模型编了一个不存在的工具 —— 回话让它改，**不能跳过**
                    return ToolMessage(
                        content=(
                            f"没有名为「{name}」的工具。可用工具："
                            f"{'、'.join(self._tools_by_name) or '（一个都没有）'}"
                        ),
                        tool_call_id=call_id,
                        name=name or "unknown",
                    )

                try:
                    # config 显式传下去：让这次工具调用挂进 astream_events 的事件树
                    # （M6 SSE 的 tool_call/tool_result 事件靠它把"直接父"判定为
                    # tool_step，从而与子 agent 内部的搜索区分开）。不传也能靠
                    # 环境回调偶然挂上，但那是实现细节，不该依赖。
                    raw = await tool.ainvoke(args, config=config)
                    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                except Exception as exc:  # noqa: BLE001 —— 异常也必须变成回应
                    # 🔴 traceback 必须进日志：SSE 层只给模型/用户看异常类名，
                    #    不留堆栈的话，偶发异常（如 2026-09-18 实测的 AttributeError）
                    #    永远钉死不到行。
                    logger.error("工具 %s 执行失败（args=%s）", name, args, exc_info=True)
                    text = (
                        f"工具「{name}」执行失败：{type(exc).__name__}: {exc}\n"
                        f"可以先换个关键词或换个工具试试，不要因此就凭记忆编数据。"
                    )

                return ToolMessage(content=text, tool_call_id=call_id, name=name)

            # ── 并行执行（D76）──
            # 一轮里的多个工具调用若互不依赖（模型同轮派出的多个 `task` 搜索任务、
            # 批量查天气），串行 await 就是纯等待 —— 2026-09-18 实测 60% 耗时在这。
            # `gather` 并发跑；各自的安全网不变：
            # · 高德限速是"取号式"模块级排队（单 loop 原子），并发自动串成 2.2 QPS 队列
            # · 池子/台账/trace 是同 context 内追加，无共享写冲突
            # 预算超限的调用不进 gather，直接回拒绝消息（语义不变）。
            # ToolMessage 顺序按原 tool_calls 顺序回填 —— 顺序变了模型对不上号。
            over_budget = [c for i, c in enumerate(calls) if i >= allowed]
            in_budget = [c for i, c in enumerate(calls) if i < allowed]

            over_budget_msgs = [
                ToolMessage(
                    content=TOOL_BUDGET_TEXT,
                    tool_call_id=str(c.get("id") or ""),
                    name=str(c.get("name") or "") or "unknown",
                )
                for c in over_budget
            ]

            if in_budget:
                run_msgs = list(
                    await asyncio.gather(*(_run_one(c) for c in in_budget))
                )
            else:
                run_msgs = []

            # 回填：预算内的按原顺序排前面，超限拒绝消息按原顺序接在后面
            results.extend(run_msgs)
            results.extend(over_budget_msgs)

            snapshot = _pool_dump(active)
            sub_trace = drain_subagent_trace()

        update: dict[str, Any] = {
            "messages": results,
            "tool_call_count": len(calls),
            "collected_pois": snapshot,
            "subagent_trace": sub_trace,  # add reducer → 只追加本次新增，历史由 state 保着
            "searched_keywords": keywords_ledger,  # D71：覆盖语义（列表本身已累计）
        }
        # ⚠️ 工具执行失败**不写** `state["error"]`（2026-09-18 修）：
        # 工具错误是**模型可自愈的瞬时错误** —— 失败文本已经通过 ToolMessage 回喂模型
        # （它下一轮就会改对重试），UI 也已经通过 ToolResultEvent(ok=False) 展示。
        # 写进 state["error"] 的后果：模型重试成功后错误**仍然留在终态**，
        # 流结束被当成最终错误发 ErrorEvent —— 用户看到行程和判据都完整给出了，
        # 末尾却甩一行"工具执行出错"，行程到底成没成功说不清。
        # `state["error"]` 只留给**真致命**的失败（模型调用失败、草稿不合格等）。
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
            # 池空有**两种成因**，必须分开说（D67-A）：
            #   ① 数据源覆盖不到这个目的地 → 说清是数据源的限制（并给出出路）
            #   ② 覆盖得到，但模型这轮没调 / 调的词都搜不到 → 说模型侧的问题
            # 旧文案把两者混成一句"模型没有成功调用过 search_poi"，
            # 于是①看起来像"模型偷懒"，误导排查方向。
            destination = (state.get("requirements") or {}).get("destination")
            if not self.provider.covers(destination):
                return {"error": _no_coverage_error(self.provider.name, destination)}
            return {
                "error": (
                    "候选池是空的 —— 模型这一轮没有成功调用过 search_poi"
                    "（一次都没调，或调了但每一次都是空结果）。"
                    f"数据源 {self.provider.name}，目的地「{destination or '未提供'}」。"
                )
            }

        req = Requirements.model_validate(state.get("requirements") or {}).with_defaults()
        hint = state.get("repair_hint") or None

        base: list[AnyMessage] = [
            SystemMessage(content=PLAN_SYSTEM_PROMPT),
            HumanMessage(content=build_plan_prompt(req, pool, hint)),
        ]
        llm = self.llm_plan

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

            draft, last_err = parse_draft(text_of(raw))
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
