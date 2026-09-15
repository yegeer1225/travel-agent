"""
数据契约 —— 全项目唯一的一份「行程长什么样」。

三方共用：agent 产出它 / 后端存它 / 前端渲染它。
对应文档：`技术方案.md` 第三节。冻结前请逐字段过一遍。

────────────────────────────────────────────────────────────
⚠️ 三条硬规则（改任何一个字段名 = 前后端双倍成本）
────────────────────────────────────────────────────────────
1. 这份文件是**唯一事实源**。前端 TS interface 由它翻译而来，
   后端 API 响应、agent 结构化输出、数据库 trip_json 列全都用它。
2. **不用 `Optional` 表达"这个字段高德给不了"** —— 给不了的值一律 `None`，
   同时校验层必须标 `unknown`，**绝不填占位内容**（风险 5/6：模型会编）。
3. 字段来源必须能追溯（见每个字段的注释）。**没有来源的字段一律不许加**。

────────────────────────────────────────────────────────────
🔄 2026-09-15 实测修正（写代码前必读，两条推翻了旧结论）
────────────────────────────────────────────────────────────
· 天气 `casts` 实测返回 **4 天**（不是旧文档写的 3 天）。复测 2 次一致。
  但**能保证的仍只有"从今天起 4 天内"**，超出范围照样 `status=unavailable`。
· `biz_ext.cost` / `biz_ext.open_time` 实测是 **`str | list` 双类型**：
  餐饮/咖啡返回 `'73.00'` / `'11:00-02:00'`，酒店/地铁站返回 **空数组 `[]`**。
  → 解析层必须兼容两种类型，空数组一律当 `None`（**不能当 0**）。
  实测样本：火锅 cost='73.00' open='11:00-02:00'；酒店 cost=[] open=[]；地铁站 rating/cost/open 全空。
· 校验 `status` 的合法取值是 **三态 `pass / fail / unknown`**（`方案.md` 6.2、`界面设计.md` 2.1）。
  旧示例里出现的 `"warn"` 是笔误，已删除。**软判据的"有风险"用 `fail` + `level=soft` 表达**
  （level 决定要不要打回重排，status 决定显示什么颜色）。
"""

from __future__ import annotations

import re
from datetime import date as Date
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# ══════════════════════════════════════════════════════════════
#  〇、枚举 —— 前端会按这些生成 TS union type
# ══════════════════════════════════════════════════════════════


class TripSource(StrEnum):
    """行程从哪来。决定界面二能不能"继续聊"（A37）。"""

    GENERATED = "generated"  # 冷启动：对话生成 → session_id 非空，可继续聊改
    PASTED = "pasted"  # 热启动：粘贴现成攻略 → session_id 为 None，只能确定性改


class CheckLevel(StrEnum):
    """判据等级 —— 决定要不要打回重排，不决定颜色。"""

    HARD = "hard"  # 硬判据：fail 就打回 L2 重排（上限 2 次）
    SOFT = "soft"  # 软判据：fail 只提示，不阻塞


class CheckStatus(StrEnum):
    """校验三态（`方案.md` 6.2）。**只有这三个值，没有第四个**。

    · PASSED  : 确认没问题
    · FAILED  : 确认有问题（hard→打回；soft→提示）
    · UNKNOWN : 数据拿不到，判不了 —— **不允许退化成 pass**
    """

    PASSED = "pass"
    FAILED = "fail"
    UNKNOWN = "unknown"


class WeatherStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"  # 出发日超出预报窗口 / 地理编码失败 / 接口报错


class CoordSys(StrEnum):
    """坐标系。高德返回 GCJ-02，与前端 JS API 地图天然一致，不需要转换。"""

    GCJ02 = "GCJ-02"


class AuthorType(StrEnum):
    """内容是"人"发的还是"系统"发的。系统内容没有 user_id（NULL）。"""

    USER = "user"
    SYSTEM = "system"


class Visibility(StrEnum):
    """**只有 `guide_posts` 这张表有这个字段**（A27）。

    其余表一律默认私有，靠 `WHERE user_id` 隔离 —— 给每张表都加 visibility
    会让人误以为存在公开读路径，从而漏写 WHERE。
    """

    PRIVATE = "private"
    PUBLIC = "public"  # 用户点「发布」后置为 public 并写 published_at


# ══════════════════════════════════════════════════════════════
#  一、可复用的字符串类型
# ══════════════════════════════════════════════════════════════

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _check_hhmm(v: str) -> str:
    if not _HHMM.match(v):
        raise ValueError(f"时间必须是 24 小时制 HH:MM，收到 {v!r}")
    return v


HHMM = Annotated[str, AfterValidator(_check_hhmm)]
"""`"09:00"` 这样的 24 小时时间。**不做时区处理** —— 行程全部按目的地的本地时间算。"""


# ══════════════════════════════════════════════════════════════
#  二、行程契约（主）—— 前端要渲染的全部
# ══════════════════════════════════════════════════════════════


class Weather(BaseModel):
    """某一天的天气。来源：高德天气接口 `extensions=all`。

    ⚠️ 两个实测约束：
    1. 参数是 `city=adcode`（城市编码，成都 510100），不是城市名
       → 工具内部先用地理编码转 adcode，**不额外增加工具数量**
    2. 预报**只覆盖从今天起的 4 天**，没有"按指定日期查"的能力
       → 出发日在窗口外时 `status=unavailable` + `note` 写原因，**不标 pass**
    """

    model_config = ConfigDict(extra="forbid")

    status: WeatherStatus
    day_weather: str | None = None  # 白天天气，如"晴"
    night_weather: str | None = None
    day_temp: int | None = None
    night_temp: int | None = None
    day_wind: str | None = None  # 风向，如"东北风"
    day_power: str | None = None  # 风力，如"3"（原样字符串，别转数字）
    report_time: str | None = None  # 预报发布时间，如"2026-09-15 20:36:13"
    note: str | None = None  # unavailable 时写原因，直接显示给用户看


class Check(BaseModel):
    """一条判据的结果。**行程里最值钱的可视化**（面试会盯着问）。

    硬 5 类 / 软 4 类见 `方案.md` 6.1。
    """

    model_config = ConfigDict(extra="forbid")

    level: CheckLevel
    code: str  # 判据标识，前端按它查文案；新判据直接加，不用改前端
    status: CheckStatus
    msg: str | None = None  # 人话解释。`unknown` 时必填（要说清为什么判不了）


class Stop(BaseModel):
    """行程里的一站。"""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)  # 当天内的顺序，从 1 开始
    name: str
    poi_id: str
    """高德 POI ID —— **这是"不是编的"的证据**，也是封闭世界校验的锚点。
    模型写的任何地点都必须能在这里找到对应 POI，否则 hard/fail。"""

    lng: float
    lat: float
    coord_sys: CoordSys = CoordSys.GCJ02

    arrive: HHMM | None = None
    stay_min: int = Field(ge=0)
    leave: HHMM | None = None

    from_prev_km: float = Field(default=0, ge=0)
    """距上一站的**路径**距离（km）。**代码算**，不是模型估的。当天第 1 站为 0。"""

    from_prev_drive_min: int = Field(default=0, ge=0)
    """距上一站的车程（分钟）。**代码算**（高德测距）。当天第 1 站为 0。"""

    cost_per_person: float | None = None
    """高德 `biz_ext.cost` —— **是"人均消费"不是"门票"**，卡片上别写"门票"。

    ⚠️ 实测：只有餐饮/咖啡类可靠（'73.00'）；酒店、地铁站返回空数组 `[]`。
    空数组 → `None`，**且不要显示"暂无"**（风险 5：缺就隐藏）。"""

    rating: str | None = None
    """高德 `biz_ext.rating`，如 '4.8'。**保持字符串**（原样透传，不转 float）。
    实测：只有餐饮/酒店/景点/影院类有，地铁站等为 `[]`。"""

    open_time: str | None = None
    """高德 `biz_ext.open_time`，如 '11:00-02:00'。

    ⚠️ 实测与 cost 同样是 `str | list` 双类型。**有值时 `open_today` 硬判据可做**；
    为空时该判据只能标 `unknown`（不是 pass）。"""

    match_reason: str | None = None
    """**LLM 生成**，接口给不了。一句话说清"为什么这一站适合这个用户"。"""

    checks: list[Check] = Field(default_factory=list)


class DayStats(BaseModel):
    """一天的汇总数字。**全部代码算**，确定性，可重算（界面二拖拽后要重算）。"""

    model_config = ConfigDict(extra="forbid")

    distance_km: float = Field(ge=0)
    drive_min: int = Field(ge=0)
    walk_km: float = Field(ge=0)


class Day(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: int = Field(ge=1)  # 第几天，从 1 开始
    date: Date | None = None
    """⚠️ 可空：粘贴来的行程（source=pasted）经常没有明确日期。
    冷启动生成时**必有**（日期是阻塞流程的必问项，A25）。"""

    theme: str | None = None  # 一句话主题，如"市区古迹·走得少"
    weather: Weather | None = None  # 拿不到就是 None（或 status=unavailable）
    stops: list[Stop] = Field(default_factory=list)
    day_stats: DayStats | None = None


class TripSummary(BaseModel):
    """给"行程卡"和"路线总览"页顶部用的汇总。**全部代码算**。"""

    model_config = ConfigDict(extra="forbid")

    total_distance_km: float = Field(ge=0)
    total_cost_per_person: float | None = None
    """人均总花费。⚠️ 只在**有 cost 的站点上累加**（餐饮/咖啡），
    酒店/交通站点无数据 → 不要按 0 算进去假装精确。全无数据时为 None。"""

    stop_count: int = Field(ge=0)
    hard_errors: int = Field(ge=0)
    soft_warnings: int = Field(ge=0)  # 软判据中 status=fail 的条数


class ValidationIssue(BaseModel):
    """校验过程中"修掉的"或"没修掉的"一个问题。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    msg: str = Field(description="人话，直接显示给用户")
    level: CheckLevel | None = None  # `fixed` 里可省；`remaining` 里必须有


class Validation(BaseModel):
    """这一轮生成了几轮、修了什么、还剩什么。**这是 agent 过程可视化的一半素材**。"""

    model_config = ConfigDict(extra="forbid")

    rounds: int = Field(default=0, ge=0)
    """被打回重排了几轮（L3 上限 2 次，超了降级输出 + 列出剩余风险）。"""

    fixed: list[ValidationIssue] = Field(default_factory=list)
    remaining: list[ValidationIssue] = Field(default_factory=list)


class Trip(BaseModel):
    """**行程契约根对象**。等同于数据库 `trips.trip_json` 列的完整内容。

    前端拿到它就能画出界面二的表格 + 地图 + 按天卡片；
    PATCH 改完顺序后，后端重算 `day_stats/summary` 再回吐同一个结构。
    """

    model_config = ConfigDict(extra="forbid")

    trip_id: str
    session_id: str | None = None
    """归属会话。⚠️ **`source=pasted` 时必为 None**（A37：粘贴不建会话）。

    后果：`/trips/{id}` 的访问控制**不能靠 session 反查**，
    必须用 `owned_trip()` 直接按 `trips.user_id` 校验（见 `技术方案.md` 第三节末）。"""

    user_id: int = 1
    """⚠️ 现在写死 1；M9 起由 JWT 里的值决定。行程默认私有。"""

    title: str
    destination: str  # 目的地城市名，如"成都"
    source: TripSource
    created_at: datetime
    updated_at: datetime

    days: list[Day] = Field(default_factory=list)

    summary: TripSummary
    validation: Validation = Field(default_factory=Validation)


# ══════════════════════════════════════════════════════════════
#  三、后端内部结构 —— 高德返回值的「清洗后」形态
#     不直接给前端，但 agent 工具和 providers 门面共用它
# ══════════════════════════════════════════════════════════════


class AmapPoi(BaseModel):
    """**清洗后**的 POI。上游是高德 `/v3/place/text` 的原始返回。

    为什么要有这一层：高德原始字段的类型不稳定（见下），
    必须在 provider 里收敛成确定类型，**模型和校验层才不用防坑**。

    ⚠️ 实测的类型陷阱（原始值 → 清洗后）：
      · `biz_ext.cost`      `'73.00'` 或 `[]`   → `float | None`
      · `biz_ext.open_time` `'11:00-02:00'` 或 `[]` → `str | None`
      · `biz_ext.rating`    `'4.8'` 或 `[]`     → `str | None`
      · `location`          `'104.047992,30.646168'` → 拆成两个 float
      · `alias`             `'武侯祠|武侯祠景区'` → 拆成 list
    """

    model_config = ConfigDict(extra="forbid")

    poi_id: str
    name: str
    alias: list[str] = Field(default_factory=list)
    """别名，`|` 分隔拆开。**用于封闭世界校验的模糊匹配**（风险 3）：
    模型写「武侯祠」而池子里是「武侯祠博物馆」，靠 alias 兜住。"""

    type: str | None = None  # 如"风景名胜;风景名胜;国家级景点"
    typecode: str | None = None  # 如"110100"，可用于类型过滤
    lng: float
    lat: float
    address: str | None = None  # 可能为 []（高德会返回空数组）
    adname: str | None = None  # 区县，如"武侯区"
    cityname: str | None = None
    adcode: str | None = None
    tel: str | None = None
    photos: list[str] = Field(default_factory=list)
    """实景图 URL。**实测每个 POI 都有 3 张**，可直接作首页轮播素材（A39 / 风险 15）。
    域名实测为 `https://aos-comment.amap.com/...`，HTTP 200 / image/jpeg。"""

    rating: str | None = None
    cost_per_person: float | None = None
    open_time: str | None = None
    open_time_detail: str | None = None  # `biz_ext.opentime2`，比 open_time 详细


# ══════════════════════════════════════════════════════════════
#  四、SSE 事件契约 —— 界面一「实时看 agent 在干什么」的全部素材
#     `api.md` 的 SSE 事件表直接引用这里；前端 `lib/sse.ts` 按它解析
#     ⚠️ 浏览器 `EventSource` 只支持 GET → 聊天用 POST + fetch + ReadableStream
# ══════════════════════════════════════════════════════════════


class SSEEventType(StrEnum):
    SESSION = "session"  # 会话建立/恢复（第一条，必发）
    NODE = "node"  # 图节点的开始/结束
    TOOL_CALL = "tool_call"  # 模型决定调工具
    TOOL_RESULT = "tool_result"  # 工具返回
    TOKEN = "token"  # 最终答复的文本增量（打字机）
    TRIP = "trip"  # 完整行程（前端据此渲染行程卡/跳路线总览）
    CHECK = "check"  # 校验卡
    DONE = "done"  # 流正常结束
    ERROR = "error"  # 流内错误（HTTP 已经 200，错误只能在流里报）


class SessionEvent(BaseModel):
    type: Literal["session"] = "session"
    session_id: str
    title: str | None = None


class NodeEvent(BaseModel):
    """节点级事件 —— 工具轨迹卡的来源。**是"节点级"，不是打字机效果**。"""

    type: Literal["node"] = "node"
    node: str  # parse_intent / ask_more / agent_step / tool_step / generate_plan / check_plan / repair / render
    phase: Literal["start", "end"]
    label: str  # 后端给好中文，前端直接显示，如"正在检查行程可行性"
    elapsed_ms: int | None = None  # phase=end 时有值


class ToolCallEvent(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    call_id: str
    tool: str  # search_poi / get_weather / calc_distance（**只有这 3 个**）
    args: dict  # 原始参数，前端可折叠展示
    label: str  # "正在搜索：武侯祠"


class ToolResultEvent(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    tool: str
    ok: bool
    summary: str  # "找到 3 个候选" —— 不要把原始返回全丢给前端
    degraded: bool = False
    """MCP 调用失败、已自动降级到 httpx 直连时为 True（A23 门面降级）。
    前端可显示一个小标记，**这是"我做了降级"的唯一可观测证据**。"""


class TokenEvent(BaseModel):
    type: Literal["token"] = "token"
    text: str


class TripEvent(BaseModel):
    type: Literal["trip"] = "trip"
    trip: Trip


class CheckEvent(BaseModel):
    type: Literal["check"] = "check"
    round: int
    hard_errors: int
    soft_warnings: int
    checks: list[Check]


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    session_id: str
    trip_id: str | None = None


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str  # 如 llm_400 / amap_timeout / tool_failed
    msg: str


SSEEvent = Annotated[
    SessionEvent
    | NodeEvent
    | ToolCallEvent
    | ToolResultEvent
    | TokenEvent
    | TripEvent
    | CheckEvent
    | DoneEvent
    | ErrorEvent,
    Field(discriminator="type"),
]
"""SSE 每条消息的 `data:` 都是这个联合体之一。
前端按 `type` 字段分发，**未知 type 必须忽略而不是报错**（方便后端加新事件）。"""


__all__ = [
    # 枚举
    "TripSource",
    "CheckLevel",
    "CheckStatus",
    "WeatherStatus",
    "CoordSys",
    "AuthorType",
    "Visibility",
    "SSEEventType",
    # 工具类型
    "HHMM",
    # 行程契约
    "Weather",
    "Check",
    "Stop",
    "DayStats",
    "Day",
    "TripSummary",
    "ValidationIssue",
    "Validation",
    "Trip",
    # 内部结构
    "AmapPoi",
    # SSE 事件
    "SessionEvent",
    "NodeEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "TokenEvent",
    "TripEvent",
    "CheckEvent",
    "DoneEvent",
    "ErrorEvent",
    "SSEEvent",
]
