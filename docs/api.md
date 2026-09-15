# 接口契约（API）

> **M0 三件套之一。** 这份文件冻结后，前端（豆包）按它写请求层，后端按它写路由。
> 契约变更 = 前后端双倍成本；**改之前先读第七节**。
>
> 配套：`backend/app/schemas.py`（数据结构，**唯一事实源**）｜`docs/sample_trip.json`（真实响应样例）
> 最后更新：2026-09-15

---

## 一、通用约定（先看这节，能省一半沟通）

| 项 | 约定 |
|---|---|
| **Base URL** | dev：`http://127.0.0.1:8000/api`。前端**不要写死端口** —— Vite `server.proxy` 把 `/api` 转到后端（见 `前端交接.md`） |
| 编码 | 全部 `application/json; charset=utf-8`（**不是** `ensure_ascii` 转义；中文直接出） |
| 命名 | 字段一律 **`snake_case`**。前端拿到后**不要转成 camelCase** —— TS interface 就跟 snake_case（省掉一层转换和一类 bug） |
| 时间 | 时间戳：ISO 8601 带时区 `2026-09-15T20:50:17+08:00`；日期：`2026-10-01`；行程内时刻：`HH:MM`（`09:00`，**`9:00` 会 400**） |
| 分页 | 统一 `?limit=20&offset=0` → 统一外壳 `{items, total, limit, offset}`（`total` 是**满足条件的总数**，不是本页条数） |
| 排序 | 列表默认**倒序**（最新在前），接口不提供 `sort` 参数 |
| 幂等 | `GET` / `PATCH` 幂等；`POST` 不保证（点赞除外，它是切换语义） |

### 1.1 鉴权：前端从第一天就统一带上（M9 零改动）

| 阶段 | 后端 | 前端要做的 |
|---|---|---|
| **M5 ~ M8** | `get_current_user_id()` 直接返回常量 `1`，**不发 token** | ⚠️ **仍然统一写请求头**：`Authorization: Bearer <token>`，没登录时值为空字符串 |
| **M9** | 换成真 JWT 校验 | ✅ **一行不改**（因为头已经在发了） |

```
Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJ1aWQiOjF9.xxx
```

> 🔴 **为什么现在就要带**：如果 M6 不写这行头，M9 上线时前端所有的 `fetch` 都要改一遍 ——
> 而这正是"契约冻结后还要返工"的典型来源。**现在多写 1 行，M9 省 20 处改动。**

**JWT 里只有 `user_id` + `exp`。** 前端**不许解 token 取昵称/头像** —— 那些走 `GET /auth/me`。
理由：前端能 base64 解开 token（是编码不是加密），但**能读到不代表那是给前端用的契约**；
哪天后端往 payload 里加东西，解 token 的前端就会悄悄依赖上。

### 1.2 错误：所有非 2xx 都是同一个形状

```json
{ "error": { "code": "not_found", "msg": "会话不存在", "detail": null } }
```

| HTTP | `code` | 什么时候 | 前端该怎么表现 |
|---|---|---|---|
| 400 | `invalid_param` | 参数不合法（时间格式错、字段缺失、`ops` 为空…） | 把 `msg` 直接显示在表单下方 |
| 400 | `keyword_required` | 景点搜索关键词为空 | ❌ **不该发生** —— 前端在关键词为空时**不要发请求**，直接显示引导文案 |
| 401 | `unauthorized` | token 缺失/过期/签名错（M9 起） | 清本地 token → 跳登录 |
| 404 | `not_found` | **不存在 或 无权访问**（见下） | 显示"页面不存在"，**不要提示"无权限"** |
| 409 | `conflict` | 用户名已存在、攻略标题重复 | 显示 `msg` |
| 429 | `rate_limited` | 同一用户短时间发太多（软限流） | 按钮置灰 + 倒计时 |
| 500 | `internal_error` | 未预期异常 | 显示"服务出错了" + 可重试按钮 |
| 502 | `llm_error` | 模型调用失败（DeepSeek 400/超时） | 显示"AI 服务暂时不可用"，**保留用户已输入的内容** |
| 503 | `amap_error` | 高德接口失败/限流重试后仍失败 | 同上，但文案说明"数据源暂时不可用" |

🔴 **404 的语义是刻意的**：拿别人的 `session_id` / `trip_id` / 私有 `guide_id` 请求，**必须返回 404，不能返回 403**。
403 等于告诉攻击者"这个东西存在，只是不给你看" —— 泄露了存在性。
**前端也不要去区分这两种情况**（它本来就分辨不了），统一显示"不存在"。

### 1.3 前端只按 `code` 分支，不要匹配 `msg`

`msg` 是给人看的中文，会改；`code` 是给机器判别的，改了就是我们违约。
前端需要自定义文案时，**按 `code` 查自己的文案表**。

### 1.4 mock 模式：前端无感知

后端 `.env` 里 `AMAP_PROVIDER=mock` 时，**接口路径、请求体、响应结构完全不变**，
只有「数据是不是真的」变了。所以：

- 豆包**不用等后端写完**，M1 一完成就能开工
- 前端**不需要写任何 `if (mockMode)` 分支** —— 有分支就说明契约没冻结
- 判断当前是不是 mock：`GET /health` 的 `mock_mode`

---

## 二、接口总表

标注：**🟢 真做**｜**🟡 占位可用**（结构就绪，数据后补）｜**🔵 骨架**（先能跑通，逻辑后置）

阶段对应 `技术方案.md` 的里程碑（M4 景点 / M5 会话 / M6 助手页 / M7 路线总览 / M8 评测 / M9 用户 / M10 攻略 / M11 评论点赞）。

### 2.1 系统

| 方法 | 路径 | 阶段 | 说明 |
|---|---|---|---|
| GET | `/health` | **M1** 🟢 | 联调探活。`{ok, mock_mode, model_tool, model_plan, amap_configured}` |

### 2.2 会话与对话

| 方法 | 路径 | 阶段 | 说明 |
|---|---|---|---|
| GET | `/sessions` | M5 🟢 | 我的会话列表（分页）→ `Page<Session>` |
| POST | `/sessions` | M5 🟢 | 新建空会话。body `{title?}` → `Session` |
| GET | `/sessions/{id}` | M5 🟢 | 会话 + 全部历史消息 → `SessionDetail` |
| DELETE | `/sessions/{id}` | M5 🟢 | 删会话。**不删行程**（行程是独立资产，个人中心还要列） |
| POST | `/sessions/{id}/chat` | **M6** 🟢 | **SSE**。body `ChatRequest` → 事件流 |

### 2.3 行程（核心）

| 方法 | 路径 | 阶段 | 说明 |
|---|---|---|---|
| GET | `/trips` | M5 🟢 | 我的行程列表 → `Page<TripSummaryItem>`。**个人中心「我的 AI 路线规划记录」用它** |
| GET | `/trips/{id}` | M5 🟢 | 完整 `Trip`（含 days / stops / checks / summary） |
| PATCH | `/trips/{id}` | **M7** 🟢 | 拖拽/删站/改时间。body `PatchTripRequest` → **重算后的完整 `Trip`** |
| POST | `/trips/{id}/recheck` | M7 🟢 | 深度软校验（**只有它调 LLM**）→ `RecheckResponse` |
| POST | `/trips/{id}/amap-import` | M7 🔵 | 高德 APP 唤端链接 → `AmapImportResponse` |
| POST | `/trips/paste` | **M7** 🟢 | **SSE**。热启动：粘一段行程 → 校验 → 出总览（**不建会话**） |

### 2.4 景点

| 方法 | 路径 | 阶段 | 说明 |
|---|---|---|---|
| GET | `/spots/search?keywords=&city=&limit=&offset=` | M4 🟡 | 代理高德 POI 搜索（带进程内缓存）→ `SpotSearchResponse` |
| GET | `/spots/{poi_id}` | M8 🟡 | POI 详情 → `SpotCard` |

### 2.5 首页

| 方法 | 路径 | 阶段 | 说明 |
|---|---|---|---|
| GET | `/home` | M6 🟡 | 一次拿完 Hero + 猜你喜欢 → `HomeResponse` |

### 2.6 攻略社区 / 互动

| 方法 | 路径 | 阶段 | 说明 |
|---|---|---|---|
| GET | `/guides?city=&keywords=&mine=` | M10 🟡 | 列表。默认**只返回 public**；`mine=1` 返回自己的全部 → `Page<GuideListItem>` |
| POST | `/guides` | M10 🟢 | 创建（**默认 `private`**）。body `GuideUpsertRequest` → `GuideDetail` |
| GET | `/guides/{id}` | M10 🟢 | 详情 → `GuideDetail`（public 直通；private 才校验归属） |
| PATCH | `/guides/{id}` | M10 🟢 | 改（只传要改的字段）→ `GuideDetail` |
| POST | `/guides/{id}/publish` | M10 🟢 | 发布：`visibility=public` + 写 `published_at` |
| POST | `/guides/{id}/unpublish` | M10 🟢 | 撤回为 private |
| DELETE | `/guides/{id}` | M10 🟢 | 删除（自己的） |
| GET | `/guides/{id}/comments` | M11 🟢 | 评论列表 → `Page<CommentItem>` |
| POST | `/guides/{id}/comments` | M11 🟢 | 发评论。body `CommentCreateRequest` → `CommentItem` |
| DELETE | `/comments/{id}` | M11 🟢 | 删自己的评论（或自己攻略下的任意评论） |
| POST | `/likes/toggle` | M11 🟢 | **点赞/取消二合一** → `LikeState` |
| GET | `/likes?target_type=&target_id=` | M11 🟢 | 查状态 → `LikeState`（列表页要显示"赞 12"） |
| GET | `/favorites?target_type=` | M9 🟢 | 我的收藏 → `Page<FavoriteItem>` |
| POST | `/favorites` | M9 🟢 | 收藏。body `FavoriteCreateRequest` → `FavoriteItem` |
| DELETE | `/favorites/{target_type}/{target_id}` | M9 🟢 | 取消收藏 → `204` |

### 2.7 用户 / 鉴权

| 方法 | 路径 | 阶段 | 说明 |
|---|---|---|---|
| POST | `/auth/register` | M9 🟢 | body `RegisterRequest` → `AuthResponse` |
| POST | `/auth/login` | M9 🟢 | body `LoginRequest` → `AuthResponse` |
| GET | `/auth/me` | M9 🟢 | 当前用户 → `UserOut`。**昵称/头像只从这里拿** |
| PATCH | `/auth/me` | M9 🟢 | 改昵称/邮箱/头像 → `UserOut` |
| POST | `/uploads/avatar` | M9 🟢 | `multipart/form-data`，字段名 `file` → `AvatarUploadResponse` |

---

## 三、关键接口详述

只列**行为不显然**的。其余按第二节的方法名去看 `schemas.py` 里对应的类即可。

### 3.1 `POST /sessions/{id}/chat` → SSE

前端**必读** 3.7 / 第四节。

```
POST /api/sessions/3f9c.../chat
Content-Type: application/json
Accept: text/event-stream
Authorization: Bearer <token>

{ "message": "成都，3天，带爸妈", "steer": false }
```

- 响应 `Content-Type: text/event-stream`，**HTTP 状态恒为 200**（流里出错也是 200，错误走 `error` 事件）
- 后端行为：把消息存进 `messages` → checkpointer 恢复该会话状态 → `graph.astream_events(...)` → 边跑边转 SSE
- 同一个 `session_id` **同时只允许一个在跑的流**；重复发起 → 新流开始前先中断旧的（或返回 `429`，二选一，M6 时定）

### 3.2 `PATCH /trips/{id}` —— 确定性重算

```json
{
  "ops": [
    { "op": "move",        "day": 1, "seq": 3, "to_seq": 1 },
    { "op": "delete",      "day": 1, "seq": 2 },
    { "op": "update_time", "day": 2, "seq": 1, "arrive": "10:00", "stay_min": 120 }
  ]
}
```

响应 = **重算后的完整 `Trip`**（不是 diff，不是 204）。前端**整体替换**本地 state。

🔴 **为什么一次提交一批 op**：拖一下顺序 → 到达时间要重排 → 后面每站的 `arrive`/`leave` 都变。
如果每拖一次发一个请求，中间那几百毫秒前端会渲染出"时间还没重排完"的错乱状态。
**一次提交、一次重算、一次替换**，界面上永远看不到中间态。

**重算什么**（全部确定性，**不调 LLM**）：
`from_prev_km` / `from_prev_drive_min` / `arrive` / `leave` / `day_stats` / `summary` / 硬判据 `checks`。

**不重算什么**：`match_reason`（LLM 生成的，重算要钱，保留原文）、`Day.weather`（跟顺序无关）。

不动软判据：软判据里"旺季建议预约"这类跟顺序无关的结论同样保留；
顺序相关的软判据**在 `recheck` 时才更新**。

### 3.3 `POST /trips/{id}/recheck`

只有它会调 LLM（软校验）。前端表现：**按钮置灰 + loading，通常 5~15s**。
返回 `RecheckResponse`（`checks` + `validation`），**不是**完整 Trip ——
因为重排是 agent 的事，前端只更新校验卡和角标。

### 3.4 `POST /trips/paste` → SSE（热启动）

```
{ "text": "Day1 武侯祠→锦里→宽窄巷子\nDay2 ...", "destination": "成都" }
```

- **不建会话**（A37）：`trip.session_id` 为 `null`，`source` 为 `"pasted"`
- 事件流与 chat **同一套**，区别只有两条：**不发 `session` 事件**、`done.session_id` 为 `null`
- `Day.date` 常见为 `null`（粘贴的攻略经常没写日期）→ 前端**必须能渲染"无日期"的天**
- 入口二（直接粘贴）出来的行程**不能回助手页接着聊**（它没有会话），只能走确定性改
- `destination` 不传时后端从正文解析；解析不出就按校验结果里出现最多的城市

### 3.5 `GET /spots/search`

| 参数 | 必填 | 说明 |
|---|---|---|
| `keywords` | ✅ | **为空 → 400 `keyword_required`**。前端在空关键词时**不要发请求**，直接显示引导文案 |
| `city` | ❌ | 城市名（如 `成都`）；不传则全国搜 |
| `limit` / `offset` | ❌ | 默认 `20` / `0`，`limit` 上限 `50` |

- 响应带 `source`（`amap` / `mock`）和 `cached`（是否命中进程内缓存）
- 🔴 **后端必须串行 + 限速**：高德个人 Key 连续第 4 个请求就 `CUQPS_HAS_EXCEEDED_THE_LIMIT`
  （实测，见 `技术方案.md` 一节末）。**限流的响应是 `status=0` 而不是抛异常**，写漏了会被当成"搜不到"
- 🔴 **本项目是搜索页，不是列表页**：没有本地 POI 库，所以**默认态是空**，前端必须显示引导文案，
  ❌ **不许硬凑几张卡**、❌ **不许写"共收录 N 个景点"**（我们没有这个数）

### 3.6 `GET /home`

```json
{
  "hero": [ { "poi_id": "B0FFH...", "name": "宽窄巷子", "city": "成都", "photo": "https://aos-comment.amap.com/..." } ],
  "recommended": [ { "poi_id": "...", "name": "武侯祠", "city": "成都", "district": "武侯区",
                     "cost_per_person": null, "rating": "4.8", "photos": ["..."], "lng": 104.05, "lat": 30.64 } ]
}
```

- `hero` 给 **3~5 张**（少于 3 张前端**不做轮播**，直接单图）
- 素材来源 = 高德 POI 的 `photos` 字段（**真数据、可追溯**）。取不到时用无版权图库，**页脚标注来源**
- ❌ **禁止** AI 生图充当景点照片（会画出不存在的景点）、❌ 禁止灰底占位图
- `hero` 展示的景点要与 `recommended` 的 4 个**不重复**

### 3.7 SSE 契约（前端实现细节都在这里）

#### ① 帧格式（**和标准 SSE 略有不同，必须照这个来**）

```
data: {"type":"node","node":"parse_intent","phase":"start","label":"正在理解你的需求","elapsed_ms":null}

data: {"type":"token","text":"成都"}

: ping

```

| 规则 | 说明 |
|---|---|
| **只用 `data:` 行，不发 `event:` 行** | 事件类型在 JSON 的 `type` 字段里，发两遍容易不一致 |
| **每个 `data:` 是单行 JSON** | 后端必须 `ensure_ascii=False` 且**不能有换行**。否则前端按行切会断成两半 |
| **帧之间空一行** | 标准 SSE 约定，前端按 `\n\n` 切帧 |
| **心跳是 `: ping` 注释行** | 每 15s 一次，防代理超时断流。⚠️ **前端解析必须跳过以 `:` 开头的行** |
| 流正常结束 vs 断流 | **只有收到 `done` 才算成功**。EOF 而没收到 `done` = 断流 → 提示"连接中断，请重试" |

#### ② 事件表（9 种，对应 `schemas.py` 的 `SSEEventType`）

| `type` | 何时发 | payload 关键字段 | 前端渲染成什么 |
|---|---|---|---|
| `session` | chat 的**第一条**（paste **不发**） | `session_id` / `title` | 把临时会话换成真实 id（左侧历史栏插入一条） |
| `node` | 每个图节点开始/结束 | `node` / `phase`(`start`\|`end`) / `label` / `elapsed_ms` | **工具轨迹卡的一行**，`end` 到了就打勾并显示耗时 |
| `tool_call` | 模型决定调工具 | `call_id` / `tool` / `args` / `label` | 轨迹卡里"正在搜：武侯祠"（`args` 可折叠） |
| `tool_result` | 工具返回 | `call_id` / `ok` / `summary` / `degraded` | 同一行补结果"找到 3 个候选"；`degraded=true` 加个小标记 |
| `token` | 最终答复的文本增量 | `text` | **打字机效果**（只在这里） |
| `check` | 每一轮校验结束 | `round` / `hard_errors` / `soft_warnings` / `checks[]` | **校验卡**（三态分组：通过 / 不通过 / 无法判定） |
| `trip` | 行程生成/修正完成 | `trip`（完整 `Trip`） | **行程卡**（天数/站数/硬错数 + 「生成路线总览」按钮） |
| `done` | 流正常结束 | `session_id` / `trip_id` | 结束 loading；`trip_id` 有值才能跳路线总览 |
| `error` | 流内出错 | `code` / `msg` | 红色提示条，**保留用户输入** |

#### ③ 典型顺序（chat 冷启动，跑了一轮校验、被打回一次）

```
session            ← 只有 chat 有
node    parse_intent  start
node    parse_intent  end
node    ask_more      start        ← 必问项缺口 → 到这里就结束（下面不发）
node    ask_more      end
token   "成都…"                     ← 提问文本用打字机逐字出
done

── 用户第二轮 ────────────────────────────────────────────
session            （恢复会话）
node    agent_step    start
tool_call  search_poi
tool_result search_poi
tool_call  get_weather
tool_result get_weather
node    agent_step    end
node    generate_plan start
node    generate_plan end
check   round=1  hard_errors=1        ← 硬错 → 打回
node    repair        start
node    repair        end
node    agent_step    start           ← 回 L2 重排（L3 上限 2 次）
…
check   round=2  hard_errors=0
token   "行程已生成…"
trip    {…完整 Trip…}
done    session_id / trip_id
```

> ⚠️ **`token` 只在"出文本给用户"时发**。工具循环里的模型调用**不流式**（且关思考），
> 所以**不要期待在 `agent_step` 期间看到 `token`** —— 那段时间界面靠 `node` / `tool_call` 事件动起来。
> 这也正是"轨迹卡"和"打字机"是两个东西的原因。

#### ④ 前端解析参考（`src/lib/sse.ts`）

```ts
export type SSEHandler = (evt: { type: string } & Record<string, any>) => void

/** ⚠️ EventSource 只支持 GET，而聊天是 POST → 必须手写 */
export async function streamSSE(
  url: string,
  body: unknown,
  onEvent: SSEHandler,
  signal: AbortSignal,
) {
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      Authorization: `Bearer ${localStorage.getItem('token') ?? ''}`, // ← M9 之前为空串
    },
    body: JSON.stringify(body),
    signal, // ← ★ 严格模式下靠它取消，否则 effect 跑两次＝扣两次钱
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  if (!res.body) throw new Error('响应没有 body，无法流式读取')

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let gotDone = false

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })

    // 按"空行"切帧；最后一段可能不完整，留在 buf 里等下一块
    const frames = buf.split('\n\n')
    buf = frames.pop() ?? ''

    for (const frame of frames) {
      const line = frame.split('\n').find((l) => l.startsWith('data:'))
      if (!line) continue                 // ← 跳过 `: ping` 心跳
      const evt = JSON.parse(line.slice(5).trim())
      if (evt.type === 'done') gotDone = true
      onEvent(evt)                        // 未知 type 必须忽略而不是抛错
    }
  }
  if (!gotDone) throw new Error('连接中断，未收到 done')
}
```

`src/lib/api.ts` 的普通请求也**统一带 `Authorization`**：

```ts
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${localStorage.getItem('token') ?? ''}`,
      ...(init.headers ?? {}),
    },
  })
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    // 只按 code 分支，不要匹配 msg
    throw new ApiError(body?.error?.code ?? 'unknown', body?.error?.msg ?? `HTTP ${res.status}`)
  }
  return res.status === 204 ? (undefined as T) : res.json()
}
```

#### ⑤ 三个必须绕开的坑（否则线上必炸）

| 坑 | 后果 | 怎么做 |
|---|---|---|
| **React 19 严格模式 `useEffect` 跑两次** | SSE 连两次 → agent 跑两遍 → **DeepSeek 扣两次钱** | effect 里 `const ac = new AbortController()`，**`return () => ac.abort()`** |
| **Vite dev proxy 缓冲 SSE** | 事件憋到最后一起吐，看起来像"卡住然后刷屏" | proxy 配置里**不要开压缩**；dev 时确认真的是逐条到 |
| **`EventSource` 只能 GET** | 用 `EventSource` 根本发不出聊天请求 | 用上面的 `fetch` + `ReadableStream`，**永远不要用 `EventSource`** |

---

## 四、前端渲染必须遵守的两条硬规则

这两条不是前端"要不要"的问题，是**会决定项目可信度**的：

### 4.1 三态：空数据 ≠ 通过

`checks[].status` 只有 `pass` / `fail` / `unknown`：

| status | 显示 | 颜色变量 |
|---|---|---|
| `pass` | 通过 | `--color-status-pass` |
| `fail` | 不通过（`level=soft` 时是"有风险"，不打回重排） | `--color-status-fail` |
| **`unknown`** | **无法判定**（必须显示 `msg` 里写的原因） | `--color-status-unknown` |

🔴 **`unknown` 绝不允许显示成绿色通过的样子。** 这是这个项目最值钱的可视化 ——
面试官会专门问"你怎么区分'确认不对'和'不知道对不对'"。
（典型真实场景：青城山的 `biz_ext` 里没有 `open_time` → `open_today` 只能 `unknown`）

⚠️ 三态色**必须用独立变量**，不许复用装饰色板 —— 装饰绿 `#6BCB77` 和通过绿 `#16A34A` 是同一色感区间，
将来改品牌色会把三态一起毁掉。

### 4.2 没有来源的字段一律不显示

`null` 的处理是 **隐藏整个格子**，不是显示"暂无"、更不是编一个：

| 情况 | ❌ 不许做 | ✅ 怎么做 |
|---|---|---|
| `cost_per_person` 为 `null` | 显示 `¥0`（0 是"免费"，`null` 是"没数据"，是两件事） | 不显示这一格 |
| `rating` 为 `null` | 显示 `0.0` / `暂无评分` | 不显示 |
| `photo` 取不到 | 灰色占位图（看着像坏了） | 纯色块 + 站名 |
| 景点页没有搜索关键词 | 硬凑几张卡充数 | 引导文案：「输入景点名称或关键词开始搜索，例如「成都 博物馆」」 |

**禁止出现**的三个字段（高德根本没有，参考设计里那些全是自造种子数据）：
`热度` / `趣味度` / `门票价格`；以及 **「共收录 N 个景点」** 这类总数。

---

## 五、SSE 之外：不要发明接口

前端**不允许**：
- 自己拼高德接口直连（`restapi.amap.com`）—— key 会暴露在浏览器里，而且绕过了后端的限流与缓存
- 期待后端返回契约里没有的字段（`extra="forbid"`，多出来的字段后端会直接在开发期报错，这是故意的）
- 用 `localStorage` 拼一个"本地假行程"给页面用（那会让联调期的问题被掩盖到很晚）

需要新字段时的正确流程是 **第七节**。

---

## 六、错误码与 HTTP 状态码对照（实现时照抄）

```python
# 后端统一异常（backend/app/api/errors.py，M5 落地）
raise AppError("not_found", "会话不存在", 404)
raise AppError("invalid_param", "arrive 必须是 HH:MM", 400)
raise AppError("amap_error", "高德接口限流，已重试 3 次仍失败", 503)
```

FastAPI 默认的 `{"detail": ...}` 必须被 exception handler 改写成 `ErrorBody`，
**否则前端会同时面对两种错误格式** —— 这是最容易漏的一处不一致。

---

## 七、契约纪律（改之前必读）

| 动作 | 成本 | 要求 |
|---|---|---|
| 改**实现**（换模型 / 加工具 / 改 prompt / 换 checkpoint / 加缓存） | **0** | 随便改，前端零改动 |
| 改**后端内部结构**（如 `AmapPoi` 加字段、只给后端用） | **0** | 随便改 |
| **给 `Trip` / 已有响应加字段** | **前后端双倍** | 先问：前端**要不要显示**？不显示就别加进契约（放内部结构里） |
| **改字段名 / 删字段 / 改类型** | **前后端双倍 + 已写页面全返工** | 必须走下面的流程 |
| **新增响应结构**（新接口） | 低 | 加进 `schemas.py` → 重新生成类型 → 通知豆包 |

**变更流程（三步，缺一不可）**：

1. 改 `backend/app/schemas.py`（**唯一事实源**，先改这里）
2. 重新生成前端类型（一条命令，见 `前端交接.md`）→ 前端**编译期**就会报出所有受影响的地方
3. 在 `api.md` 对应位置改样例 + 在 `DECISIONS.md` 记一条（决定 / 替代方案 / 代价）

> ⚠️ **反向操作是错的**：先在 `api.md` 改文字、`schemas.py` 忘了改 ——
> 两天后两份契约就开始不一致，而**不一致是查不出来的**（没有报错，只有前端"这个字段是 undefined"）。

---

## 八、这份契约还缺什么（诚实清单）

| 项 | 状态 |
|---|---|
| 幂等/重试的具体策略（`PATCH` 带 `If-Match` 版本号？） | ❌ 未定。现阶段单用户、本地跑，**不加**。多端编辑同一行程时才会成为问题 |
| WebSocket vs SSE | ✅ SSE（单向够用）。**不需要双向**：用户消息走 POST，agent 消息走 SSE |
| 限流阈值 | ❌ 未定，M5 定（软限流，按用户+IP） |
| 文件上传的大小/类型限制 | ❌ 未定，M9 定（头像：≤2MB，`image/png\|jpeg\|webp`） |
| 移动端 | ❌ 不做（只保证 1440px）。见 `界面设计.md` 六节 |
