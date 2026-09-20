"""需求抽取：把用户那句自然语言，变成结构化的「必问 7 项」（A25）。

═══════════════════════════════════════════════════════════════
 必问 ≠ 阻塞 —— 这个区分是整个文件的存在理由
═══════════════════════════════════════════════════════════════

| 概念 | 含义 | 做错了会怎样 |
|---|---|---|
| **必问** | 这一项要主动问用户 | 问少了 = 需求不全，排出来的行程不贴人 |
| **阻塞** | 用户答不上来时**必须停下等** | 阻塞项搞多了 = **agent 卡死**，用户说"你看着办"它也不动 |

7 项全阻塞是常见错误：用户不设预算、没有特别要求，agent 就一直追问。
A25 定死：**只有「目的地」和「日期」阻塞**，其余 5 项问完可以缺省。

```
REQUIRED_FIELDS = 7 项     ← 都要问
BLOCKING_FIELDS = 2 项     ← 只有这两个卡流程
```

═══════════════════════════════════════════════════════════════
 `extra="ignore"` 而不是 `"forbid"` —— 和 schemas.py 相反，故意的
═══════════════════════════════════════════════════════════════

`schemas.py` 里所有模型都是 `extra="forbid"`，这里是 `"ignore"`。
不是因为懒，是因为**输入方不同**：

| 类 | 输入是谁给的 | 多一个字段意味着 | 该怎么处理 |
|---|---|---|---|
| `schemas.py` 的契约类 | **前后端代码** | 契约漂移，必须立刻发现 | `forbid` → 报错 |
| 本文件的抽取类 | **模型** | 它顺手多写了个 `"confidence": 0.9` | `ignore` → 丢掉就行 |

> 判据：**`forbid` 是用来保护"我自己写的代码"的，不是用来惩罚模型的。**
> 对模型输出用 `forbid`，会把"多写一个字段"升级成"整个流程失败"——
> 而模型多写字段的概率相当高。

真正需要校验的是**必填项和类型**，那些由字段声明本身保证。
"""

from __future__ import annotations

import json
import re
from datetime import date as Date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# ══════════════════════════════════════════════════════════════
#  两个常量集合（A25）—— 用 tuple 不用 set
#
#  ⚠️ 内部需求文档 4.3 里写的是 `{"destination", ...}`（set 字面量）。
#  这里改成 **tuple**，因为 set **无序** —— 而追问文案要按固定顺序排
#  （"先去哪座城市"排在"哪天出发"前面更像人话）。
#  set 的成员判断能力 tuple 一样有，所以没有损失。
# ══════════════════════════════════════════════════════════════

REQUIRED_FIELDS: tuple[str, ...] = (
    "destination",  # 1 目的地 —— 高德 city 参数、adcode、标题
    "date",  # 2 出行日期 —— 天气校验（实测只覆盖 4 天）
    "days",  # 3 天数 —— 分天、天气覆盖天数
    "travelers",  # 4 同行人 —— **走路量阈值**（带老人小孩要放宽）
    "budget",  # 5 预算 —— 只能说"人均参考"，见下方注释
    "preferences",  # 6 偏好 —— 搜什么关键词
    "notes",  # 7 特别要求 —— 忌口 / 必去 / 要避开
)

BLOCKING_FIELDS: tuple[str, ...] = ("destination", "date")
"""**只有这两个卡流程。**其余 5 项缺失走缺省值，绝不追问第二遍。"""

FIELD_LABELS: dict[str, str] = {
    "destination": "去哪座城市",
    "date": "哪天出发",
    "days": "玩几天",
    "travelers": "和谁一起去（几位大人 / 有没有老人小孩）",
    "budget": "预算大概多少（人均）",
    "preferences": "偏好什么（历史古迹 / 自然风光 / 美食 / 亲子 / 摄影）",
    "notes": "有没有特别要求（忌口、必去的、要避开的）",
}

DEFAULT_DAYS = 3
DEFAULT_TRAVELERS = "成人 ×2"


# ══════════════════════════════════════════════════════════════
#  结构
# ══════════════════════════════════════════════════════════════


class Travelers(BaseModel):
    """同行人构成。

    **为什么要分大人/小孩/老人而不是一个数字**：校验层的"累计走路量"判据看的是
    **结构**不是总数 —— 带 1 个老人和带 1 个成年朋友，走路的容忍度差一倍
    （1.2 km/站 对前者可能就偏多了）。
    """

    model_config = ConfigDict(extra="ignore")

    adults: int | None = Field(default=None, ge=0)
    children: int | None = Field(default=None, ge=0)
    elders: int | None = Field(default=None, ge=0)
    note: str | None = None

    @field_validator("adults", "children", "elders", mode="before")
    @classmethod
    def _soft_int(cls, value: Any) -> Any:
        """能读成整数就读，读不了返回 `None`。

        🔴 **2026-09-16 真实跑出来的失败**：模型返回
        `{"adults": null, "children": 0, "elders": 0}`（用户说的是"带爸妈"），
        而 `adults` 原本声明的是非空 `int` → 校验失败 →
        **整条抽取被丢掉**，连已经抽对的目的地和日期一起没了。
        用户看到的是一句"还差两个必填项，去哪座城市？" —— 而他刚说了成都。

        一个字段的毛病不该让整条结果作废。这也是"宽容"和"严格"必须分开的理由：
        **`forbid` 该用来保护我自己写的代码，不该用来惩罚模型的部分正确。**

        ⚠️ 三个数字都可空，`None` = **这一项不知道**，不是 0 ——
        "不知道有几个成人"和"没有成人"是两件完全不同的事，
        后者会让"走路量 vs 同行人"的判据按一个不存在的团去放宽阈值。
        """
        return _soft_int(value)


class Requirements(BaseModel):
    """抽取出来的需求。**每个字段都可空** —— 空 = 用户没说，不是"还没加载"。"""

    model_config = ConfigDict(extra="ignore")

    destination: str | None = None
    date: Date | None = None
    days: int | None = Field(default=None, ge=1, le=30)
    travelers: Travelers | None = None
    budget: float | None = Field(default=None, ge=0)
    """⚠️ 单位是**人均**。且高德给的是 `biz_ext.cost`（**人均消费**）不是门票，
    门票数据大面积缺失 —— 所以预算只能做"人均参考的粗略对比"，
    **做不到"精确控制人均不超 500"**。界面上要写"人均参考"，不承诺精度。"""
    preferences: list[str] = Field(default_factory=list)
    notes: str | None = None

    # ---- 宽容解析：能读就读，读不了**只丢那一个字段** ----
    #
    # ⚠️ 这些 validator 不改变字段语义，只避免"一个字段的毛病废掉整条抽取"。
    # 副作用要说清：`days=None` 现在有两种来源 ——
    #   ① 用户没提   ② 用户提了但解析不了
    # 两者在当前流程里行为一致（都走默认值），所以 M1 不区分；
    # **M3 的 eval 需要分辨"抽取失败率"时必须分开记**，那时再加一个 `unparsed` 字段。

    @field_validator("days", mode="before")
    @classmethod
    def _soft_days(cls, value: Any) -> Any:
        return _soft_int(value)

    @field_validator("budget", mode="before")
    @classmethod
    def _soft_budget(cls, value: Any) -> Any:
        return _soft_float(value)

    @field_validator("date", mode="before")
    @classmethod
    def _soft_date(cls, value: Any) -> Any:
        """能解析成日期的就留，解析不了**返回 None 而不是报错**。

        模型偶尔会原样带回"下周三""国庆"这类没换算的表述。
        直接抛校验错会让整条抽取失败；丢掉它只是回到"缺日期 → 追问"，
        而那正是这个字段原本该走的路。
        """
        if value is None or isinstance(value, Date):
            return value
        if isinstance(value, str):
            text = value.strip().replace("/", "-").replace(".", "-")
            try:
                return Date.fromisoformat(text)
            except ValueError:
                return None
        return None

    @field_validator("travelers", mode="before")
    @classmethod
    def _empty_travelers_is_absent(cls, value: Any) -> Any:
        """`{}` 或**全空对象** → `None`。

        🔴 这一条不能省。不拦住的话 `{}` 会 validate 成
        "成人/儿童/老人都是 0" 这样一个**空团** ——
        一个用户从没说过的假设，而且它会一路流到"走路量阈值"判据里。

        判据是"**有没有任何一个字段有值**"而不是"对象是不是空"：
        `{"adults": null, "elders": 2}` 是**有信息**的（两位老人），必须留。
        """
        if isinstance(value, dict) and not any(
            v not in (None, "", [], {}) for v in value.values()
        ):
            return None
        return value

    @field_validator("preferences", mode="before")
    @classmethod
    def _clean_preferences(cls, value: Any) -> Any:
        """这个字段**没有"未知"这个状态**，只有"有哪些偏好"，所以统一收敛成列表。

        顺手滤掉非字符串（模型可能塞个 dict 进来）——
        否则下游拼文案时会输出 `{'name': '美食'}` 这种东西，
        而它只在"模型今天心情不好"时才出现，很难复现。
        """
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, (list, tuple)):
            return [v.strip() for v in value if isinstance(v, str) and v.strip()]
        return []

    # ---- 派生 ----
    def missing_blocking(self) -> list[str]:
        """还缺哪些**阻塞项**。非空 → 走 `ask_more`，不进工具循环。"""
        return [f for f in BLOCKING_FIELDS if getattr(self, f, None) is None]

    def with_defaults(self) -> Requirements:
        """补齐 5 个**非阻塞**项的缺省值，然后交给生成节点。

        ⚠️ 这一步是**在进入生成之前**做的，不是抽取时就填 ——
        如果抽取阶段就填默认值，`missing_required` 会拿到一个"已经填好"
        的 `days`，于是"用户没说几天"这个事实**永久丢失**，
        追问文案里也就永远问不到它了。

        缺省值本身就是**产品决定**（A25 那张表最后一列）：
        "问完可以缺省，但不能让 agent 卡在那"。所以这里填的是
        "用户没提时系统按什么处理"，而不是"用户大概想要什么"。
        """
        data = self.model_dump()
        if data.get("days") is None:
            data["days"] = DEFAULT_DAYS
        if data.get("travelers") is None:
            # ⚠️ 必须带上 `adults` —— 填 `Travelers()`（全 None）会被
            # `_empty_travelers_is_absent` 判定成"没说"再抹回 None，等于没填。
            data["travelers"] = Travelers(adults=2).model_dump()
        if not data.get("preferences"):
            data["preferences"] = ["综合"]
        return Requirements.model_validate(data)


# ══════════════════════════════════════════════════════════════
#  抽取
# ══════════════════════════════════════════════════════════════

INTENT_JSON_HINT = """
{
  "destination": "城市名（只要城市，不要写省/区），用户没说就 null",
  "date": "YYYY-MM-DD，用户没说就 null",
  "days": 3,
  "travelers": {"adults": 2, "children": 0, "elders": 0, "note": null},
  "budget": 1500,
  "preferences": ["历史古迹", "美食"],
  "notes": "忌口 / 必去 / 要避开的；没有就 null"
}
""".strip()

INTENT_SYSTEM_PROMPT = f"""你在做「行程需求抽取」，只做抽取，**不要规划行程**。

从用户的话里取出下面这 7 项，输出一个 JSON 对象：

{INTENT_JSON_HINT}

规则：

1. **没提到的字段填 null，不要猜、不要用常见值填。**
   用户说"想去成都玩几天"，那 `days` 是 null —— 不是 3。
   抽取阶段填默认值会让"用户没说"这个事实永久丢失。
2. **相对日期要换算成绝对日期。** 我会给你今天的日期和星期作为锚点，
   "下周三"、"国庆"、"月底"都要算出 `YYYY-MM-DD`。
   算不出来的（比如"等我有空"）填 null。
3. `destination` 只要城市名。用户说"成都武侯区"就填"成都"。
4. `preferences` 是字符串数组，用户提到的每个偏好一项。
   用户说"随便看看"这类等于没说的，填空数组 `[]`。
5. 只输出 JSON，不要 markdown 代码块，不要解释。"""


_PLACEHOLDER_WORDS = {
    "",
    "null",
    "none",
    "无",
    "未知",
    "未提及",
    "未说明",
    "不清楚",
    "不知道",
    "n/a",
    "na",
}


def _is_placeholder(value: Any) -> bool:
    """**标量**占位词：`""` / `"无"` / `"未知"` / `"未提及"` 这类。

    模型非常喜欢把"没有"写成这些词，而不是老老实实写 `null`。

    ⚠️ **只看标量，不看 `[]` / `{}`。** 一开始这两个也判进去了，结果是
    `{"preferences": []}` 被抹成 `None`，然后 `list[str]` 拒绝它、整条抽取报错 ——
    而"用户没提偏好"恰恰是最常见的情况。

    空容器到底算不算"没说"，**取决于字段自己的类型**：
    `preferences=[]` 是合法的"没有偏好"，而 `travelers={}` 会 validate 成
    "成人×2 的默认团"（一个**用户根本没说过的假设**）。所以那个判断
    交给字段各自的 validator，不在这里一刀切。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _PLACEHOLDER_WORDS
    return False


def _is_absent(value: Any) -> bool:
    """更宽的一档：None / 标量占位词 / **空容器**。

    专门用在"要不要覆盖"这个判断上（`merge_requirements`）：
    用户这一轮没提偏好（`[]`）时，**不能把上一轮说的偏好清零**。
    """
    if _is_placeholder(value):
        return True
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """把标量占位词统一抹成 `None`（空容器保留原样，见 `_is_placeholder`）。"""
    return {k: (None if _is_placeholder(v) else v) for k, v in data.items()}


def _soft_int(value: Any) -> int | None:
    """尽量把模型给的值读成整数；读不了返回 `None`。

    ⚠️ **这不是"猜"**，它只做两件没有歧义的事：

    1. 去掉粘在数字上的单位：`"3天"` → 3、`"1500元"` → 1500
    2. 放行真数字类型的输入

    **不做的事**（做了就是猜）：中文数字不转（`"五天"` → `None`）、
    模糊表述不猜（`"几天"` → `None`）。宁可让上游走默认值或追问，
    也不要让"用户说了五天、系统按三天排"这种**没人能发现**的错发生。

    `bool` 显式排除：Python 里 `True` 是 `int` 的子类，
    不拦的话 `{"days": true}` 会静默变成 1 天。
    """
    if value is None or _is_placeholder(value):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if float(value).is_integer() else None
    if isinstance(value, str):
        match = re.search(r"-?\d+", value)
        if match:
            return int(match.group())
    return None


def _soft_float(value: Any) -> float | None:
    """同 `_soft_int`，但保留小数（预算可能是 `1500.5`）。"""
    if value is None or _is_placeholder(value):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if match:
            return float(match.group())
    return None


def build_intent_prompt(
    message: str,
    *,
    today: Date,
    weekday: str,
    existing: dict[str, Any] | None = None,
) -> str:
    """拼抽取用的 user prompt。

    ⚠️ **锚点日期必须显式给出**。不给出的话，模型算相对日期会以它的训练时间
    为基准 —— 那可能差好几年，而且**算错的结果看起来完全正常**
    （一个合法日期），没有任何下游环节能发现。
    """
    lines = [
        f"今天日期：{today.isoformat()}（{weekday}）",
        "",
    ]
    if existing:
        known = {k: v for k, v in existing.items() if not _is_absent(v)}
        if known:
            lines += [
                "之前几轮已经知道的信息（**这一轮用户没提到的，保持原样返回**）：",
                json.dumps(known, ensure_ascii=False),
                "",
            ]
    lines += ["用户这一轮说：", message, "", "输出 JSON："]
    return "\n".join(lines)


def parse_intent_json(raw: str | dict[str, Any]) -> tuple[Requirements | None, str | None]:
    """解析抽取结果。返回 `(需求, 错误信息)`。

    容错顺序和 `draft.parse_draft()` 一致：**先剥围栏，再 Pydantic**。
    模型即使被要求"只输出 JSON"，包一层 ``` 的概率仍然不低 ——
    这是最常见的失败形式，不是"模型不听话"。
    """
    if isinstance(raw, dict):
        payload: Any = raw
    else:
        text = (raw or "").strip()
        if text.startswith("```"):
            text = "\n".join(
                ln for ln in text.splitlines() if not ln.strip().startswith("```")
            ).strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"抽取结果不是合法 JSON：{exc}"

    if not isinstance(payload, dict):
        return None, f"抽取结果应当是 JSON 对象，收到 {type(payload).__name__}"

    cleaned = _clean(payload)
    # 只留认识的键 —— `extra="ignore"` 本来也会丢，但早丢一步能带走脏数据
    known = set(REQUIRED_FIELDS)
    try:
        return Requirements.model_validate({k: v for k, v in cleaned.items() if k in known}), None
    except ValidationError as exc:
        errs = "; ".join(
            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
        )
        return None, f"字段不符合要求：{errs}"


def merge_requirements(old: Requirements, new: Requirements) -> Requirements:
    """把新一轮抽取合并进旧需求。

    规则：**新的非空值覆盖旧的，新的空值不动旧的。**

    为什么不是"用新的整体替换"：用户第二句只说"改成 4 天"，
    整体替换会把上一轮说的目的地、同行人全部抹掉 —— 而模型**不会告诉你它抹了**，
    下一轮排出来的行程就悄悄变了目的地。
    """
    data = old.model_dump()
    for key, value in new.model_dump().items():
        if _is_absent(value):
            continue
        data[key] = value
    return Requirements.model_validate(data)


def build_ask_text(requirements: Requirements, missing: list[str]) -> str:
    """生成追问文案。

    **模板化而不是让模型措辞**：追问是确定性行为（缺哪个问哪个），
    调一次模型只会多一处可变、多一次计费、多一次可能出错的地方。
    等真的需要更自然的措辞时（M6 会话流）再换，那时它本来就有一整轮对话可挂。

    ⚠️ 2026-09-16 修过两个真实出现的排版毛病（都是第一次跑 CLI 时看到的）：
    · 什么都不缺时输出 `收到，。`（逗号后面直接跟句号）
    · 缺一项时仍然写"还差**两个**必填项"
    这类文案 bug 不会被任何断言抓住，只有人眼能看到 —— 所以"跑一次看看"
    这件事不能省。
    """
    known: list[str] = []
    if requirements.destination:
        known.append(f"目的地「{requirements.destination}」")
    if requirements.date:
        known.append(f"出发日 {requirements.date.isoformat()}")

    if known:
        prefix = f"收到，{'、'.join(known)}。"
    elif requirements.destination is None:
        prefix = "好的。"
    else:
        prefix = "收到。"

    count = {1: "一项", 2: "两项"}.get(len(missing), f"{len(missing)} 项")
    items = "\n".join(f"{i}. {FIELD_LABELS[f]}？" for i, f in enumerate(missing, start=1))
    tail = "\n\n其余信息（天数、同行人、预算、偏好）不说也行，我会按常规默认安排。"
    return f"{prefix}还差{count}必填项就能开始排：\n\n{items}{tail}"


__all__ = [
    "BLOCKING_FIELDS",
    "DEFAULT_DAYS",
    "DEFAULT_TRAVELERS",
    "FIELD_LABELS",
    "INTENT_JSON_HINT",
    "INTENT_SYSTEM_PROMPT",
    "REQUIRED_FIELDS",
    "Requirements",
    "Travelers",
    "build_ask_text",
    "build_intent_prompt",
    "merge_requirements",
    "parse_intent_json",
]
