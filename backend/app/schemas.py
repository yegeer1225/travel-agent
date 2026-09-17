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
from typing import Annotated, Generic, Literal, TypeVar

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
    """内容是"人"发的还是"系统"发的。系统内容没有 user_id（NULL）。

    ⚠️ 现状：**只有 `USER` 会被产出** —— `api/views.py` 唯一实现点硬编码 `"user"`，
    因为 DB 层 `guides.user_id` 是 NOT NULL、也没有这一列（见 `技术方案.md` 3.3 的
    「设计有、实现无」块）。`SYSTEM` 是留给"系统发布的真实攻略"的扩展位，**尚未打通**。
    """

    USER = "user"
    SYSTEM = "system"


class Visibility(StrEnum):
    """**只有 `guides` 这张表有这个字段**（A27）。

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

    checks: list[Check] = Field(default_factory=list)
    """**天级判据**（M3 补的字段）。默认空，所以老数据（M5 之前存的 `trip_json`）照样能读。

    为什么 `Stop.checks` 不够 —— 有两类判据的**主语是"这一天"而不是"某一站"**：

    · `weather_conflict`：暴雨撞上户外景点。罚的是"这一天这么排"，不是某一站
    · `walk_load`：当天累计走路量超出同行人的承受范围。同上

    硬把天级判据挂到某个 `Stop` 上会出两个问题：判据的语义被扭曲；
    同一份错误在一天里重复 N 次（前端会显示 5 个一模一样的红标）。

    ⚠️ 这是 **M0 冻结后的一次契约变更**，理由和时点都记在 `DECISIONS.md D37`：
    变更发生在**前端动手之前**，成本最低；不加这个字段，
    `方案.md` 6.1 里那两条硬判据**没有地方安放**。"""


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

    checks: list[Check] = Field(default_factory=list)
    """**行程级判据**（M3 软判据补的字段，D46）。默认空 → 老数据照样能读。

    为什么还要这一层 —— 判据的**主语**有三种，`Stop.checks` 和 `Day.checks` 各盖不住第三种：

    | 落点 | 主语 | 例子 |
    |---|---|---|
    | `Stop.checks` | 这个地点 | `poi_exists` / `open_today` / `reachable` / `needs_booking` |
    | `Day.checks` | 这一天 | `weather_conflict` / `walk_load` / `elder_friendly` |
    | **`Trip.checks`** | **整份行程** | **`overall_feasible`**（"三天 11 个站，节奏偏满"） |

    ⚠️ 和 `Day.checks` 同一个道理：把行程级判据塞进某一天，
    会让"这句话说的是整份行程"变成"某一天的问题"——语义被扭曲，
    而且如果挂在第一天，用户会以为"只要改第一天就行"。

    变更时点同 D37：**发生在前端动手之前**，成本最低。理由见 `DECISIONS.md` D46。"""


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
    SESSION = "session"  # 会话建立/恢复（**chat 的第一条，必发**；paste 不发，见下）
    NODE = "node"  # 图节点的开始/结束
    TOOL_CALL = "tool_call"  # 模型决定调工具
    TOOL_RESULT = "tool_result"  # 工具返回
    TOKEN = "token"  # 最终答复的文本增量（打字机）
    TRIP = "trip"  # 完整行程（前端据此渲染行程卡/跳路线总览）
    CHECK = "check"  # 校验卡
    DONE = "done"  # 流正常结束
    ERROR = "error"  # 流内错误（HTTP 已经 200，错误只能在流里报）


class SessionEvent(BaseModel):
    """会话建立 / 恢复。

    ⚠️ **`/sessions/{id}/chat` 必发第一条；`/trips/paste` 不发** ——
    粘贴走的是热启动，**不建会话**（A37），前端不能假设这条事件一定到。
    """

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
    """流正常结束。

    ⚠️ 前端判断"这一轮成功"**只看这条事件**，不要靠 HTTP 层的 EOF ——
    代理/网络断开也会让流结束，两者必须能区分（EOF 而没收到 done = 断流，要提示重试）。
    """

    type: Literal["done"] = "done"
    session_id: str | None = None  # ⚠️ paste 时**没有会话**，只能是 None（A37）
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


# ══════════════════════════════════════════════════════════════
#  五、HTTP 接口的请求 / 响应结构 —— `docs/api.md` 引用本节的类名
#     ⚠️ 规则：api.md 里出现的每一个 JSON 结构，都必须在这里有定义。
#        否则前后端就各拿一份契约，"冻结"就成了空话。
# ══════════════════════════════════════════════════════════════


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """分页响应的统一外壳。**所有列表接口都长这样**，前端只写一次解包逻辑。"""

    items: list[T] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)  # 满足条件的**总数**，不是本页条数
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class ErrorDetail(BaseModel):
    code: str
    """机器可判别的错误码（见 `docs/api.md` 错误码表）。前端**按 code 分支**，
    不要匹配 msg —— msg 是给人看的，会改。"""

    msg: str
    detail: dict | None = None  # 可选的结构化补充，前端可以不处理


class ErrorBody(BaseModel):
    """**所有非 2xx 响应的唯一格式**（FastAPI 默认的 `{"detail": ...}` 会被改写成它）。

    ⚠️ SSE 流里的错误**不走这里** —— HTTP 已经是 200 了，错误只能在 `error` 事件里报。
    """

    error: ErrorDetail


# ── 会话与消息 ──────────────────────────────────────────────


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ToolCallRecord(BaseModel):
    """历史消息里"当时调了哪些工具"—— 前端靠它把轨迹卡画回来。"""

    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict
    ok: bool
    summary: str
    degraded: bool = False


class MessageMeta(BaseModel):
    """`messages.meta_json` 列的结构。

    🔴 **没有它，历史会话点开就只剩几行文字**，轨迹卡 / 行程卡 / 校验卡全丢 ——
    而"看得见 agent 在干活"是这个项目最主要的差异点，历史里丢一半等于白做。
    """

    model_config = ConfigDict(extra="forbid")

    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    trip_id: str | None = None  # 这条回复产出的行程（有则前端可画"行程卡"）
    checks: list[Check] = Field(default_factory=list)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    role: MessageRole
    content: str
    meta: MessageMeta | None = None
    created_at: datetime


class Session(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    title: str
    model: str | None = None
    """本会话用的 LLM（A45/D55）。**None = 跟随后端 `.env` 默认**；
    选定后会话内固定，前端选择器只在建会话时出现。"""
    created_at: datetime
    updated_at: datetime


class SessionDetail(BaseModel):
    """`GET /sessions/{id}` 的响应 —— 恢复一次历史对话要的全部内容。"""

    model_config = ConfigDict(extra="forbid")

    session: Session
    messages: list[ChatMessage] = Field(default_factory=list)


class SessionCreateRequest(BaseModel):
    """`POST /sessions` 的 body（M5；M7.5 加 `model`，A45/D55）。

    🔴 `title` 可省 —— 省略时后端给默认标题（"新的行程规划"），
    **而不是报错**：用户第一句话还没说，哪来的标题？
    首轮 chat 的意图抽取会把目的地拼进去（M6），那时再改名才符合直觉。

    🔴 `model` 可省 —— 省略/None = 跟随后端 `.env` 默认。传了就**在建会话时
    fail-fast 校验**（在 `MODEL_REGISTRY` 且凭据已配），不在 chat 运行时炸。
    `model` 名单是**运行时配置**不是契约（后端加模型不改这里），
    所以前端选择器的选项来自后端配置页/约定，types.ts 里它是 `string | null`。
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=40)


class TripSummaryItem(BaseModel):
    """`GET /trips` 的列表项（M5）—— 个人中心「我的 AI 路线规划记录」的行程卡。

    ⚠️ **刻意不带 `days`**：列表页画不出也用不上完整行程，
    把 `days` 塞进列表 = 每页 20 份完整行程 JSON 的序列化 + 传输浪费。
    要看详情走 `GET /trips/{id}`（这也让"列表轻、详情重"成为缓存边界）。
    """

    model_config = ConfigDict(extra="forbid")

    trip_id: str
    session_id: str | None = None
    """归属会话（`source=pasted` 时为 None，A37）。"""
    title: str
    destination: str
    source: TripSource
    created_at: datetime
    updated_at: datetime
    summary: TripSummary


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    steer: bool = False
    """True = 插队（agent 干活途中注入意见，走 `interrupt()`）。
    默认 False = 新的一轮。前端上"边跑边改主意"的输入框才置 True。"""


class PasteTripRequest(BaseModel):
    """热启动：粘一段现成行程 / 攻略。**不建会话**（A37）。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=10, max_length=20000)
    destination: str | None = None  # 不填则从正文里解析；解析不出就按校验结果里最靠前的城市


# ── 行程改动（确定性重算）──────────────────────────────────


class TripOpKind(StrEnum):
    MOVE = "move"
    DELETE = "delete"
    UPDATE_TIME = "update_time"


class TripOp(BaseModel):
    """界面二的每一次改动 = 一个 op。

    💡 **只做这三个。** "加一站"走对话（对 agent 说"加个青城山"），
    不在界面上做 —— 否则前端要自己实现"从候选池里挑一个 POI"，等于把选点逻辑搬前端。

    🔴 **`move` 支持跨天**（D44 / A41）。跨天不是"多一个参数"这么轻 ——
    它会让**所有依赖日期的东西失效**：`Day.weather`、`open_today` 判据、
    跨天段判据、整条时间轴。重算清单见 `docs/api.md` 3.2。
    """

    model_config = ConfigDict(extra="forbid")

    op: TripOpKind
    day: int = Field(ge=1)
    seq: int = Field(ge=1)  # 目标站当前的序号
    to_seq: int | None = Field(default=None, ge=1)  # 仅 move：移到哪个序号
    to_day: int | None = Field(default=None, ge=1)
    """仅 move：**移到哪一天**。留空 = 同日内重排。

    ⚠️ 跨天时 `to_seq` 的语义是"插进目标天之后成为第几站"（1-based）；
    目标天原有的站会被顺延。**不是"和那一站交换"** —— 交换会让两站的
    `stay_min` / `arrive` 跟着串位，用户看到的是"时间乱了"。
    """

    arrive: HHMM | None = None  # 仅 update_time
    stay_min: int | None = Field(default=None, ge=0)  # 仅 update_time


class PatchTripRequest(BaseModel):
    """**一批 op 一次提交**，不是每拖一次发一个请求。

    理由：拖一下 = 时间要重排 = 后半天全变。逐个提交会算出中间态，前端会闪。
    """

    model_config = ConfigDict(extra="forbid")

    ops: list[TripOp] = Field(min_length=1, max_length=50)


class RecheckResponse(BaseModel):
    """`POST /trips/{id}/recheck` 的响应 —— 深度软校验（**只有它调 LLM**）。"""

    model_config = ConfigDict(extra="forbid")

    trip_id: str
    checks: list[Check] = Field(default_factory=list)
    validation: Validation


class AmapImportResponse(BaseModel):
    """`POST /trips/{id}/amap-import` —— 高德 APP 唤端链接（MCP 的能力，不是画图）。"""

    model_config = ConfigDict(extra="forbid")

    url: str
    note: str | None = None


# ── 景点 / 首页 ────────────────────────────────────────────


class SpotCard(BaseModel):
    """景点卡片 / 首页推荐用的**精简** POI。

    为什么不直接吐 `AmapPoi`：那张卡上只放得下 6 个字段，
    全量透传会让前端以为"必须显示全部"，也会把内部结构变成对外契约。
    """

    model_config = ConfigDict(extra="forbid")

    poi_id: str
    name: str
    city: str | None = None
    district: str | None = None  # adname，如"武侯区"
    address: str | None = None
    lng: float
    lat: float
    cost_per_person: float | None = None  # ⚠️ 人均消费，**不是门票**
    rating: str | None = None
    photos: list[str] = Field(default_factory=list)
    typecode: str | None = None


class SpotSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SpotCard] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    source: Literal["amap", "mock", "local"] = "local"
    """数据来源。**`GET /spots/search` 现在恒为 `local`**（收录库，D70）——
    景点页的语义是"搜本站收录的景点"，所以**没有回落**：收录库里没有就是空列表，
    不静默换成高德实时结果（那会让同一页混两种来源，D34 的"不静默换源"同理）。

    `amap` / `mock` 保留在枚举里给**详情的回落路径**用（见 `GET /spots/{poi_id}`），
    前端按值分支、**不要假设只有一个取值**。"""

    cached: bool = False
    """命中进程内缓存。⚠️ `source="local"` 时**恒 false** —— 本地查询毫秒级，
    原来的进程内缓存只为省高德配额（D35 的 0.45s 出站限速），本地路径不需要它。"""


class HeroSlide(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poi_id: str
    name: str
    city: str
    photo: str  # 高德 photos[0]；取不到时前端显示纯色块 + 站名，**不用灰图**


class HomeResponse(BaseModel):
    """首页一次请求拿完（Hero + 猜你喜欢）。**一页一个请求**，前端少一个 loading 态。"""

    model_config = ConfigDict(extra="forbid")

    hero: list[HeroSlide] = Field(default_factory=list)
    recommended: list[SpotCard] = Field(default_factory=list)


# ── 攻略社区 / 收藏 ────────────────────────────────────────


class TargetType(StrEnum):
    """收藏 / 点赞 / 评论**共用的多态目标**（`comments.target_type`、`likes.target_type`）。

    加一种可点赞的东西 = 加一个枚举值，不用新建表、不用改前端组件。
    """

    GUIDE = "guide"
    POI = "poi"
    COMMENT = "comment"


class GuideListItem(BaseModel):
    """攻略列表卡。`author_name` 由后端拼好 —— 前端不做 `author_type` 分支渲染。"""

    model_config = ConfigDict(extra="forbid")

    guide_id: str
    title: str
    summary: str  # 正文前 80 字，**后端截**（前端截会出现半句话 + 一堆换行）
    destination: str | None = None
    cover: str | None = None
    author_name: str  # author_type=system 时是"官方"
    author_type: AuthorType
    visibility: Visibility
    like_count: int = Field(default=0, ge=0)
    comment_count: int = Field(default=0, ge=0)
    published_at: datetime | None = None
    created_at: datetime

    source_trip_id: str | None = None
    """这条攻略由哪条行程发布而来（D61）。`None` = 手写或脚本播种的，不是发布的。

    用途两条：① 前端可显示"来自行程"并跳回原行程；② `GET /guides?source_trip_id=`
    的"是否已发布过"判据靠它命中 `idx_guides_src`。

    ⚠️ **它只溯源，不加唯一约束** —— 同一条行程允许发布多篇（同个地方可以有多个
    行程方案，D61）。"一次请求只生效一次"是另一件事，靠 `idempotency_key`。"""


class GuideDetail(GuideListItem):
    model_config = ConfigDict(extra="forbid")

    content_md: str
    poi_ids: list[str] = Field(default_factory=list)
    liked: bool = False  # 当前用户是否点过赞（未登录恒 False）


class GuideUpsertRequest(BaseModel):
    """创建 / 修改攻略。PATCH 时**只传要改的字段**（`exclude_unset`）。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=100)
    content_md: str | None = None
    destination: str | None = None
    cover: str | None = None
    poi_ids: list[str] | None = None


class PublishGuideFromTripRequest(BaseModel):
    """把一条**已有的行程**发布成攻略（D61）。**正文由后端渲染，这份请求不收正文。**

    为什么没有 `visibility` / `publish` 字段：这个端点的语义就是"发布"——
    点它 = 用户想分享，固定 `public` + `published_at=now`，**不做草稿分支**
    （少一个分支少一组测试）。想先存草稿请走 `POST /guides`（那条默认 private）。
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=100)
    """不传就用行程标题；传了就是攻略标题（攻略可以比行程标题更"文章化"）。"""

    destination: str | None = Field(default=None, max_length=100)
    """不传就用行程目的地。"""


class CommentItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment_id: str
    target_type: TargetType
    target_id: str
    author_name: str
    author_type: AuthorType
    content: str
    created_at: datetime
    is_mine: bool = False  # 前端据此决定显不显示"删除"


class CommentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=500)


class LikeToggleRequest(BaseModel):
    """一个接口干两件事：没点过就点赞，点过就取消。返回**最终状态**。

    好处：前端不用猜自己现在处于哪一态，也不用发两个请求。
    ⚠️ 并发下靠 `likes` 表的**唯一索引**兜底（`(user_id,target_type,target_id)`），
    重复插入报 IntegrityError 就当"已经点过"，不报 500。
    """

    model_config = ConfigDict(extra="forbid")

    target_type: TargetType
    target_id: str


class LikeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: TargetType
    target_id: str
    liked: bool
    count: int = Field(ge=0)  # 先 `COUNT(*)` 现算，不加冗余字段（见 `技术方案.md` 三节）


class FavoriteCreateRequest(BaseModel):
    """收藏。`name`/`cover` 是 **poi 目标的快照**（`FavoriteRepo` 注释）：

    - guide：后端自己从 `guides` 表取标题/封面，传了也**忽略**
    - poi：我们没有本地 POI 库（A31），名字只存在于收藏那一瞬 →
      前端从当前 SpotCard 传 `name`（必传，缺了 400）
    - comment：后端取评论内容前 50 字
    """

    model_config = ConfigDict(extra="forbid")

    target_type: TargetType
    target_id: str
    name: str | None = Field(default=None, max_length=200)
    cover: str | None = None


class FavoriteItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: TargetType
    target_id: str
    name: str  # 后端 join 出来，前端不做二次请求
    cover: str | None = None
    created_at: datetime


# ── 用户 / 鉴权（M9）──────────────────────────────────────


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=6, max_length=72)
    """⚠️ 上限 72 是 **bcrypt 的硬限制**（超了会被静默截断，等于密码变短）。
    这里挡住比在哈希函数里报错好排查。"""

    nickname: str | None = Field(default=None, max_length=32)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str


class UserOut(BaseModel):
    """**永远不含 `password_hash`。** 这个类就是"哪些字段能出网"的白名单。"""

    model_config = ConfigDict(extra="forbid")

    id: int
    username: str
    nickname: str | None = None
    email: str | None = None
    avatar: str | None = None
    created_at: datetime


class AuthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    expires_in: int  # 秒。前端据此决定何时清 token（**不做 refresh**，见技术方案三节） 
    user: UserOut


class UpdateProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nickname: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=128)
    avatar: str | None = None


class AvatarUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str


class HealthResponse(BaseModel):
    """`GET /health` —— 豆包联调第一步就调它，用来判断"后端起没起、是不是 mock"。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    mock_mode: bool
    """**生效档**是不是 mock。⚠️ 它与 `amap_provider_requested` 不是一回事。"""
    model_tool: str
    model_plan: str
    amap_configured: bool
    """`.env` 里配了高德 Key 没有。⚠️ **配了也可能没生效** —— 看 `amap_degraded`。"""
    amap_provider_requested: str
    """`.env` 里**写**的档（`mock` / `real`）。"""
    amap_degraded: bool
    """是否因缺凭据被**启动期降级**（D66）。
    `true` = 你配的是 `real`，但实际在跑 `mock` —— 这时任何"看起来不对"的结果
    都先往这里看一眼，别去翻 `.env`。"""


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
    "MessageRole",
    "TripOpKind",
    "TargetType",
    # 工具类型
    "HHMM",
    # 行程契约
    "Weather",
    "Check",
    "Stop",
    "DayStats",
    "Day",
    "TripSummary",
    "TripSummaryItem",
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
    # HTTP 请求/响应（api.md 引用）
    "Page",
    "ErrorDetail",
    "ErrorBody",
    "ToolCallRecord",
    "MessageMeta",
    "ChatMessage",
    "Session",
    "SessionCreateRequest",
    "SessionDetail",
    "ChatRequest",
    "PasteTripRequest",
    "TripOp",
    "PatchTripRequest",
    "RecheckResponse",
    "AmapImportResponse",
    "SpotCard",
    "SpotSearchResponse",
    "HeroSlide",
    "HomeResponse",
    "GuideListItem",
    "GuideDetail",
    "GuideUpsertRequest",
    "CommentItem",
    "CommentCreateRequest",
    "LikeToggleRequest",
    "LikeState",
    "FavoriteCreateRequest",
    "FavoriteItem",
    "RegisterRequest",
    "LoginRequest",
    "UserOut",
    "AuthResponse",
    "UpdateProfileRequest",
    "AvatarUploadResponse",
    "HealthResponse",
]
