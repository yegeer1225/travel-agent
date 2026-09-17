# 接口契约（API）

> **M0 三件套之一。** 这份文件冻结后，前端（豆包）按它写请求层，后端按它写路由。
> 契约变更 = 前后端双倍成本；**改之前先读第七节**。
>
> 配套：`backend/app/schemas.py`（数据结构，**唯一事实源**）｜`docs/sample_trip.json`（真实响应样例）
>
> 🔒 **已冻结：2026-09-15**（用户过签 7 项关键判断；原「悬而未决」的 6 项全部闭合，决定性内容见 `DECISIONS.md` D25~D31）
> 最后更新：**2026-09-17**

**契约变更记录（按时间倒序）**

| 日期 | 改了什么 | 影响 | 决策 |
|---|---|---|---|
| 2026-09-17 | `GET /spots/search` 数据源改为**收录库**；`SpotSearchResponse.source` 枚举新增 **`local`**（现在恒为它）；`cached` 恒 `false`；新增 3.5.1 说明 `GET /spots/{poi_id}` 的回落顺序 | 前端：`source` 判等要加上 `local`；**空结果现在代表"本库没收录"，不再代表"高德搜不到"** → 空态文案要改 | A47 / D70 |
| 2026-09-16 | `guides` 加 `source_trip_id` / `idempotency_key`；`POST /trips/{id}/publish-as-guide` | 详见第十四节 | A46 / D61~D63 |

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
| 幂等 | `GET` / `PATCH` 幂等（同输入同输出）；**`POST` 不保证**，但**靠唯一索引不会重复写**（点赞/收藏）；🔴 **不要靠"重发一次"来重试 `POST`** —— 见 1.5 |
| 限流 | **按接口成本分层**（最贵的 `chat` 最严）。命中返回 `429` + **`Retry-After` 头** + body 带 `detail.retry_after`，前端**倒计时后重试**。完整阈值表见 1.5 |

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

### 1.1.1 CORS（联调配置，非契约）

后端已放行 `http://localhost:5173` 与 `http://127.0.0.1:5173`（Vite 默认端口）。
前端 dev server 起在**其他端口**时提出来加，**不要自己用代理绕**。
生产部署若前后端同源，后端直接删掉 CORS 中间件。

### 1.1.2 鉴权矩阵：游客能碰到哪一步（2026-09-17 实读代码）

🔴 **这张表决定"游客能逛到哪一步"，是用来对齐"注册引导"的。**
判定依据是**端点代码里有没有 `Depends(get_current_user_id)`** —— 不是设计意图，是实读结果。

| 端点 | 游客（无 token / token 失效） | 说明 |
|---|---|---|
| `GET /health` | 🟢 公开 | |
| `GET /home` | 🟢 公开 | 首页 Hero + 猜你喜欢 |
| `GET /guides` | 🟢 公开 | 攻略列表（默认只返回 public） |
| `GET /guides/{id}` | 🟢 公开 | 攻略详情 |
| `GET /guides/{id}/comments` | 🟢 公开 | 评论列表 |
| `GET /likes?target_type=&target_id=` | 🟢 公开 | 查点赞状态 |
| `GET /spots/search` | 🔴 **401** | 景点搜索（限流键也含 `user_id`） |
| `GET /spots/{poi_id}` | 🔴 **401** | 景点详情 |
| `POST /sessions/{id}/chat` | 🔴 **401** | 聊天（最贵接口） |
| 其余全部 —— 写操作、`/auth/me`、`/uploads/*`、`/favorites`、`/trips`、`/sessions` | 🔴 **401** | |

⚠️ **`/spots` 这一对（search + detail）都落在"要登录"那侧** —— 但后果比看上去轻：
`Spots.tsx` 关键词为空时**不发请求**（`Spots.tsx:87-95`），所以游客点导航栏「旅游景点」
**能正常进页面**（看到搜索表单 + 引导文案），**只有真正提交搜索 / 点景点详情时才撞 401**。
定案见 1.1.4。

**没有 `POST /auth/logout`，这是刻意的**：JWT 无状态，登出 = 前端删 `localStorage.token`。
代价要认：**已签发的 token 在 `exp` 之前依然有效**（改密码也不能强制下线）。
单机作品这是合理取舍 —— 要真能强制下线就得引黑名单，那笔账记在 D67。

### 1.1.3 401 的统一处理：能重放的弹窗，不能重放的跳页

**判据只有一句：登录成功后能不能自动把原操作接上。**

| 触发 | 401 后怎么办 | 为什么 |
|---|---|---|
| `GET`（景点搜索、景点详情） | 🟡 **弹窗**（不跳页）→ 登录成功 → **自动重放原请求** | 重放是安全的，用户看到"登录完，结果自己出来了" |
| 写操作（收藏 / 点赞 / 评论 / 发攻略） | 🔴 **跳登录页**（带 `redirect`） | 写操作**不重放**（见下），弹窗盖上去只能让他"去登录"，登录完还得再点一次 —— **纯打断** |
| `POST` 聊天流（SSE） | 🔴 **跳登录页** | 同上（同属不能重放的写路径） |
| 页面级（「AI路线规划助手 / 路线总览 / 个人中心」） | 🔴 **跳登录页**（`RequireAuth`） | 这几页是"我的数据"，没登录没有内容可看 |

**`GET` 弹窗要这么做：**

1. `lib/auth.ts` 的 `handleUnauthorized()` **不再 `window.location.assign('/login')`**，
   改为**打开一个全局登录弹窗**：当前页留在下层，**不卸载、不清滚动位置**
   （⚠️ 原来是 `window.location.assign` —— **整页刷新**，比 SPA 跳页丢得更多）
2. 登录成功后 **自动重放刚才失败的那个请求** —— 用户看到的是"登录完，景点详情自己出来了"
3. 重放**只做一次**。再 401 说明 token 有问题：弹窗里显示错误，不再重放（防死循环）
4. **重放只对 `GET` 生效**。`POST/PATCH/DELETE` 401 后**不自动重放** ——
   重放写操作有重复提交风险（后端唯一索引是兜底，不是许可）
5. 🔴 弹窗的开/关**由用户动作驱动**，不能在"发请求时"设置 ——
   ⚠️ 弹窗**只能开一个**：并发 401（页面一次发多个 GET）必须共享同一个"等待登录"的 Promise，
   否则 N 个请求 = N 个弹窗，且只有最后一个的重放能成功（前面的 resolve 被覆盖）

⚠️ 写操作跳页用 `window.location.assign` 是**可以接受**的（与 GET 的要求相反）—— 用户本来
就要离开当前页去登录。能接进路由（`navigate()`）更好，但不强求。

✅ **本节已按上述实现（2026-09-17，见 `前端交接.md` 15.9 / 15.10）。**

### 1.1.4 ✅ 已定案（2026-09-17）：`/spots` 那一对**不拆**（B 方案）

**用户拍板：后端不动** —— `GET /spots/search` 与 `GET /spots/{id}` 都保持要登录。

| 选项 | 结论 |
|---|---|
| A. 拆开（search 改公开） | ❌ **不采用**。要动限流键（`user_id` → IP），为一个"其实没坏"的东西改限流不划算 |
| **B. 不拆** | ✅ **采用**。什么都不改 |

**为什么"不拆"是安全的**（实读代码得出的，不是估计）：

- `Spots.tsx:87-95`：关键词为空时**直接 return、不发请求** → 游客点「旅游景点」**不会撞 401**，
  看到的是搜索表单 + 引导文案，**不是空白页**
- 只有**提交搜索**或**点景点详情**才需要登录

**对游客开放的范围（2026-09-17 定稿）**：

| 页面 | 游客 |
|---|---|
| 首页 / 景点搜索页 / 攻略列表 / 攻略详情 / 评论列表 | 🟢 可进 |
| AI路线规划助手 / 路线总览 / 个人中心 | 🔴 跳登录页（"我的数据"页，没登录没内容） |

> ⚠️ 早先记过的"**导航栏各入口都允许进入**"**已被用户否掉**，别再拿它当依据。

→ 前端**不做特殊处理**：401 按 1.1.3 的两分法走。
→ 后端**零改动**。

### 1.2 错误：所有非 2xx 都是同一个形状

```json
{ "error": { "code": "not_found", "msg": "会话不存在", "detail": null } }
```

| HTTP | `code` | 什么时候 | 前端该怎么表现 |
|---|---|---|---|
| 400 | `invalid_param` | 参数不合法（时间格式错、字段缺失、`ops` 为空…） | 把 `msg` 直接显示在表单下方 |
| 400 | `keyword_required` | 景点搜索关键词为空 | ❌ **不该发生** —— 前端在关键词为空时**不要发请求**，直接显示引导文案 |
| 401 | `unauthorized` | token 缺失/过期/签名错（M9 起） | 清本地 token。**`GET` → 弹登录弹窗（不跳页）+ 登录后自动重放**；**写操作 / SSE / 页面级 → 跳登录页** —— 见 1.1.3 |
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

### 1.4 数据源档位：前端无感知

后端 `.env` 里 `AMAP_PROVIDER=real|mock`（**默认 `real`**）时，
**接口路径、请求体、响应结构完全不变**，只有「数据是不是真的」变了。所以：

- 前端**不需要写任何 `if (mockMode)` 分支** —— 有分支就说明契约没冻结
- 判断当前生效档：`GET /health` 的 `mock_mode`
- ⚠️ 配 `real` 但缺 `AMAP_WEBSERVICE_KEY` 时后端**不会报错**，而是启动日志警告 +
  降级为 mock。这时 `mock_mode=true` **且** `amap_degraded=true` ——
  **看到 `amap_degraded=true` 就知道「配的是真数据、实际在跑演示数据」**（D66）

### 1.5 重试 / 限流 / 上传 —— 前端要做什么，不要做什么

**后端已经替你重试的**（前端**无感**，不要自己再包一层重试）：

| 后端自动重试 | 次数 | 哪些错误会重试 |
|---|---|---|
| 高德 POI 搜索 / 天气 / 距离 | 3 次（退避 1s→2s→4s） | 限流（`status=0` 的 QPS 错误）、超时、5xx |
| LLM 调用 | 3 次 | `429`、超时、5xx |

**`400` / `401` / `403` 一律不重试** —— 同样的请求重试一次结果还是一样，只多烧一次配额。
所以前端看到 `400 invalid_param`，**要做的是改参数，不是重试**。

**限流阈值表**（超出 → `429`，`Retry-After` 秒数在头和 body 里都有）：

| 接口 | 维度 | 阈值 |
|---|---|---|
| `POST /sessions/{id}/chat` | 用户 | **10 / 小时 · 30 / 天** |
| **同一 session 并发流** | 会话 | **同时只允许 1 个**（第二个 → `429`） |
| `POST /trips/paste` | 用户 | **20 / 小时** |
| `POST /trips/{id}/recheck` | 用户 | **60 / 小时** |
| `GET /spots/search` | 用户 | **120 / 分钟** |
| `POST /auth/register`·`login` | IP | **10 / 分钟** |
| `POST /users/me/avatar` | 用户 | **5 / 小时** |
| 全局兜底 | IP | **300 / 分钟** |

```json
{
  "error": {
    "code": "rate_limited",
    "msg": "操作太频繁，请 42 秒后重试",
    "detail": { "retry_after": 42 }
  }
}
```

前端表现：**按钮置灰 + 倒计时**，`retry_after` 秒后自动恢复。**不要静默重试**（用户会以为按钮坏了）。
⚠️ 不要用头里的 `Retry-After` 做唯一来源 —— **`fetch` 拿它要 `Access-Control-Expose-Headers`**，所以 `detail.retry_after` 才是主路径。

**SSE 断线**：不重发请求，走**恢复**。

```
每帧都带          id: <递增序号>
重连时请求头带     Last-Event-ID: 12       ← 后端从 13 号继续推，不重头跑
```

浏览器原生 `EventSource` 会自动带这个头，但我们**用的是 `fetch` + `ReadableStream`**（`EventSource` 只支持 GET），
所以要**自己在 `src/lib/sse.ts` 里读 `id:` 并保存**（参考实现见 3.7 ④）。
🔴 **恢复粒度是"节点"不是"token"** —— 断在一个模型节点中途时，那一段会重新打字，这是预期行为不是 bug。
（后端实现排在 M6；前端现在就要把 `id:` 存下来，否则 M6 加恢复功能时前端要返工。）

**头像上传**（`POST /users/me/avatar`，M9 落地）：

| 项 | 约定 |
|---|---|
| 上限 | **10MB**（前端可以先拦一道，给出中文提示，省一次往返） |
| 类型 | `image/jpeg` · `image/png` · `image/webp`——🔴 **不接受 SVG、GIF**（前端 `accept` 属性也要照这个写） |
| 前端**不需要**压缩 | 后端会统一重编码为 **512×512 WebP**（10MB → 约 30~60KB）。前端压缩是重复劳动，且压得不如后端稳 |
| 返回 | 重编码后的**新 URL** —— 前端要**用返回值刷新头像**，不能继续用本地 `File` 的 blob URL（刷新页面就没了） |

---

## 二、接口总表

标注：**🟢 真做**｜**🟡 占位可用**（结构就绪，数据后补）｜**🔵 骨架**（先能跑通，逻辑后置）

阶段对应 `技术方案.md` 的里程碑（M4 景点 / M5 会话 / M6 助手页 / M7 路线总览 / M8 评测 / M9 用户 / M10 攻略 / M11 评论点赞）。

### 2.1 系统

| 方法 | 路径 | 阶段 | 说明 |
|---|---|---|---|
| GET | `/health` | **M1** 🟢 | 联调探活。`{ok, mock_mode, model_tool, model_plan, amap_configured, amap_provider_requested, amap_degraded}` —— 后两个是 D66 加的：`amap_degraded=true` 表示「配了 real 但缺 Key，实际在跑 mock」 |

### 2.2 会话与对话

| 方法 | 路径 | 阶段 | 说明 |
|---|---|---|---|
| GET | `/sessions` | M5 🟢 | 我的会话列表（分页）→ `Page<Session>` |
| POST | `/sessions` | M5 🟢 | 新建空会话。body `{title?, model?}` → `Session`。**`model` 可省**（= 跟随后端 `.env` 默认）；选定后会话内固定（A45）。省/传 null 走默认；传了但不在可选面或凭据未配 → `400 invalid_param` |
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
| POST | `/trips/{id}/publish-as-guide` | **M12** 🟢 | **行程 → 攻略**（`A46`）：渲染成公开攻略 → `GuideDetail`。可选头 `Idempotency-Key`，详见 3.8 |

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
| GET | `/guides?city=&keywords=&mine=&source_trip_id=` | M10/M12 🟡 | 列表。默认**只返回 public**；`mine=1` 返回自己的全部；**`source_trip_id=<行程id>` 返回"我在这条行程下发过的"**（隐含只看自己，匿名 `401`；发布前确认框的判据，见 3.8）→ `Page<GuideListItem>` |
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
| POST | `/favorites` | M9 🟢 | 收藏。body `FavoriteCreateRequest`（guide/comment 的 name 后端自己解析；**poi 必传 `name`** —— 收藏时刻的快照，无本地 POI 库）→ `FavoriteItem` |
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
- 同一个 `session_id` **同时只允许一个在跑的流**；重复发起 → **返回 `429` `rate_limited`**（已定，见 1.5）。
  前端**不要**在流还在跑时让"发送"按钮可点 —— 先本地置灰，429 只是兜底

### 3.2 `PATCH /trips/{id}` —— 确定性重算

```json
{
  "ops": [
    { "op": "move",        "day": 1, "seq": 3, "to_seq": 1 },
    { "op": "move",        "day": 1, "seq": 3, "to_day": 3, "to_seq": 1 },
    { "op": "delete",      "day": 1, "seq": 2 },
    { "op": "update_time", "day": 2, "seq": 1, "arrive": "10:00", "stay_min": 120 }
  ]
}
```

🔴 **`move` 支持跨天**（D44）：带 `to_day` 就是移天。**不带 `to_day` = 同日内重排**。

⚠️ 跨天时的两个语义坑：

| 坑 | 定死的做法 |
|---|---|
| `to_seq` 是"插进去"还是"交换" | **插入**（1-based）。目标天原有的站顺延。**不是交换** —— 交换会让两站的 `arrive`/`stay_min` 串位，用户看到的是"时间乱了" |
| 拖到某天之后那天的首站是谁 | 被插入的站**成为目标天的新首站**（这正是 D43「第一站就是出发点」的落地手段） |

响应 = **重算后的完整 `Trip`**（不是 diff，不是 204）。前端**整体替换**本地 state。

🔴 **为什么一次提交一批 op**：拖一下顺序 → 到达时间要重排 → 后面每站的 `arrive`/`leave` 都变。
如果每拖一次发一个请求，中间那几百毫秒前端会渲染出"时间还没重排完"的错乱状态。
**一次提交、一次重算、一次替换**，界面上永远看不到中间态。

**重算什么**（全部确定性，**不调 LLM**）：
`from_prev_km` / `from_prev_drive_min` / `arrive` / `leave` / `day_stats` / `summary` /
硬判据 `checks`（**`Stop.checks` 和 `Day.checks` 都要重算** —— 见 4.1）。

🔴 **跨天 `move` 要多重算这些**（D44）：

| 多算什么 | 为什么 | 拿不到时 |
|---|---|---|
| `Day.weather`（**被拖入和被拖出的那两天**） | 换了日期，天气就不是原来那个了 | 超出高德 4 天窗口 → `null`，判据落 `unknown` |
| `open_today` 判据 | 到达的是另一个日期 | 无营业时间 → `unknown`（照旧） |
| 跨天段判据 | 前一天最后一站变了 | — |

⚠️ **别把 `Day.weather` 当成"跟顺序无关"。** 同日内重排它确实不变，
但**跨天移动会让它变** —— 这是两个分支，不能一条规则盖住。

⚠️ **判据重算必须幂等**：后端每次都会**先清空再重填**。
不清的话，同一份行程校验两次 → 每个判据出现两遍 → 前端满屏重复红标，
而这个 bug **只在"重算"路径上出现**（首次生成看不出来），前端要能据此报 bug 而不是自己 dedupe。

**不重算什么**：`match_reason`（LLM 生成的，重算要钱，保留原文）。

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
| `city` | ❌ | 城市名（如 `成都`）；不传则全库搜。**用包含匹配**（库里存「成都市」、传「成都」也能命中） |
| `limit` / `offset` | ❌ | 默认 `20` / `0`，`limit` 上限 `50` |

🔴 **2026-09-17 语义变更（D70 / A47）：搜的是「本站收录库」，不再代理高德。**

```json
{
  "items": [ { "poi_id": "B001C07VJ2", "name": "成都武侯祠博物馆", "city": "成都市",
               "district": "武侯区", "address": "武侯祠大街231号", "lng": 104.047992,
               "lat": 30.646168, "cost_per_person": null, "rating": "4.8",
               "photos": ["https://aos-comment.amap.com/..."], "typecode": "140100" } ],
  "total": 1,
  "source": "local",
  "cached": false
}
```

| 字段 | 取值 | 说明 |
|---|---|---|
| `source` | **`local`** | 收录库。`amap` / `mock` 保留在枚举里给**详情的回落**用 —— 前端按值分支，**不要假设只有一个取值** |
| `cached` | **恒 `false`** | 本地查询毫秒级，没有缓存层（原来的进程内缓存只为省高德配额） |

🔴 **收录库里没有 → 返回空列表，绝不回落高德实时搜索。** 这是刻意的（D70）：

- 景点页的语义是"搜**本站收录的**景点"。回落会让同一页混两种来源，前端没法解释
  "这条为什么来自高德"；更糟的是它把"库里确实没有"这条真实路径**掩盖掉**（与 D67 同理）
- 收录库由 `backend/scripts/seed_spots.py` 写入（**只收真实高德返回**，不手写），
  当前收录内容用 `python scripts/seed_spots.py --list` 查

**排序依据**（两条，都可解释）：名称**前缀命中**优先 → **评分降序**（缺评分排最后，**不当 0 分**）。
⚠️ 排序**不用"热度"** —— 高德没有这个字段（D20）。

~~后端必须串行 + 限速（高德 QPS）~~ → **本地查询已无出站**，那条约束对搜索路径不再适用；
限流 120/分钟·用户（D28）仍保留，但它现在守的是库不是高德配额。

### 3.5.1 `GET /spots/{poi_id}`

**收录库优先 → 回落 provider → 都没有 404 `not_found`。**

与 search 的"不回落"是两回事：这是"**按 id 拿一条**"，行程卡片 / 收藏 / 攻略里的
`poi_id` 可能指向**未收录**的地点（那些是高德侧的 id），此时 404 会显得像 bug。

🔴 **本项目是搜索页，不是列表页**：默认态是空，前端必须显示引导文案，
❌ **不许硬凑几张卡**、❌ **不许写"共收录 N 个景点"**（前端拿不到真实收录总数 ——
真要显示，得后端加接口，现在**没加**）。

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
tool_call  task                        ← M4 起搜索走子 agent（主 agent 无 search_poi）
tool_result task
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

### 3.8 `POST /trips/{id}/publish-as-guide` —— 行程 → 攻略（M12 · `A46`）

把一条**已有的行程**渲染成攻略。**正文由后端从行程渲染，这份请求不收正文。**

**请求体**（整体可省）

```json
{ "title": "成都两日 · 古迹与熊猫", "destination": "成都" }
```

两个字段都可省 —— 省略时用行程自己的值。`title` 超 100 字会被后端截断（`guides.title` 列宽 100，而 `trips.title` 是 200）。

**可选头 `Idempotency-Key`**

| 情况 | 行为 |
|---|---|
| 没带 | 照常新建一篇（curl / 脚本不必造 key） |
| 带了 · 首次到达 | 新建一篇，把 key 一起存下 |
| 带了 · 同 `(user_id, key)` 已存在 | **返回原来那一篇，不新建也不报错**（HTTP 仍是 201） |
| 超过 64 字符 | `400 invalid_param` |

**key 的粒度是「一次点击意图」，不是「一条行程」** —— 这条是契约，前端必须照做：

> 在**每次用户触发发布的 click handler 内**生成一个新 uuid 放进 `Idempotency-Key`；
> 该次意图的**所有重试必须沿用同一个 uuid**；请求结束后作废。
>
> ❌ 禁止在组件渲染体里生成（每次重渲染 key 都变，"重试"会被当成"新意图"）
> ❌ 禁止每次重试重新生成（等于没做幂等）
> ✅ 请求进行中必须 `disabled` 按钮 —— 那挡的是**双击**，而**双击不属于幂等键的职责**：
> 两次 click 会生成两个 key，只能在 UI 层拦

为什么这样切：后端看两个请求长得一模一样（同用户、同行程、同 body），**没有任何信息**能区分"网络重试"和"我又想发一篇"。判定权只能在客户端 —— 同 key = 重试（返回原篇），新 key = 有意再发（照常新建）。**后端只执行"同 key 同结果"，不猜意图。**

**响应** `GuideDetail`（201）。要点：

- `visibility` 恒为 `public`、`published_at` 已写 —— 这个端点**没有草稿分支**（`D63`）
- `source_trip_id` = 源行程 id，**只溯源、没有唯一约束**：同一行程允许发多篇（同一个地方可以有多个行程方案）
- `cover` 取**首站**的高德照片；**拿不到就是 `null`**，绝不会因此让发布失败
- `content_md` 里**不含 `checks`** —— 攻略给人看，不是校验报告
- `poi_ids` = 全行程去重后的 poi_id 列表

**错误**：行程不存在或不属于你 → `404 not_found`（与全局 404 语义一致，**不区分"不存在"和"不是你的"**）；限流 → `429`（60 / 小时）。

**发布前的确认框（前端必须做，见 `D62`）**

点"发布为攻略"时**先打一次** `GET /guides?source_trip_id=<trip_id>`：

- 返回 0 篇 → 直接发
- 返回 N 篇 → 弹确认框（列出最近一篇的标题与时间 + "再发一篇"），用户确认后才发

⚠️ **判据必须每次点击现查，不能用页面加载时缓存的本地状态** —— 本地状态一定会过期（别的标签页发过、自己发过又忘了），过期时弹窗会谎报"没发过"，这个兜底就白做了。

**互转的另一半**：攻略 → 路线走 `POST /trips/paste`（把 `content_md` 喂进去即可，**后端零改动**）。

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
（典型真实场景：青城山的 `biz_ext` 里没有 `open_time` → `open_today` 只能 `unknown`。
**2026-09-16 真跑 3 天行程就出了 1 条**，这不是边角情况。）

⚠️ 三态色**必须用独立变量**，不许复用装饰色板 —— 装饰绿 `#6BCB77` 和通过绿 `#16A34A` 是同一色感区间，
将来改品牌色会把三态一起毁掉。

#### 判据挂在**两个层级**上（D37）

| 挂在哪 | 字段 | 判据的**主语** | 例子 |
|---|---|---|---|
| `Stop.checks` | 站级 | "这一站" | `poi_exists` / `open_today` / `reachable`（距上一站） |
| `Day.checks` | 天级 | "**这一天**" | `weather_conflict`（暴雨撞户外景点）/ `walk_load`（当天累计走路量）/ 当天累计车程 |

前端渲染时**别混**：天级判据显示在**当天卡片的头部**（跟"当日 4.0 km / 车程 26 分钟"同一行），
站级判据显示在**站点行后面**。
⚠️ 同一个 `code` 会在两处出现（`reachable` 既有"这一跳到得了吗"也有"这一天开太多了"），
**靠挂载位置区分，不要靠 `code` 去重** —— 去重会把其中一条真判据吃掉。
三态说明一律读 `msg`（后端给好了人话），前端不做文案分支。

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

### 4.3 「总里程」的口径 —— **看起来像 bug，但不是**（D43）

```
summary.total_distance_km = 从「当天第一站」起算的站间里程之和
                            ↓
        "住处 → 第一站"这一段**不存在** —— 因为第一站就是出发点
```

**所以这个数字是对的。不要修，也不要"补上住处到第一站的距离"。**

拿真实数据感受一下它的量级：

| | 值 |
|---|---|
| 含都江堰 + 青城山的 3 天行程，`total_distance_km` | **10.7 km** |
| 而「锦里 → 都江堰」**一段**的实测驾车距离 | **63.9 km** |

差额不是算错，是**口径**：跨天那段（回住处 + 第二天出发）不属于"站间里程"。

| 谁负责 | 做什么 |
|---|---|
| **后端** | 只算站与站之间；`Stop.from_prev_km` 对**当天第 1 站恒为 0**（这是定义不是 bug） |
| **前端** | 标签就叫「**总里程**」（不改文案）；不要自作主张加"不含…"的副标，也不要按"全天里程"去理解它 |
| **用户** | 自己把第一站设成实际出发地点 —— 界面二的拖拽重排（含跨天，见 3.2）就是干这个的 |

⚠️ **判据口径与展示口径是两件事**（D38）：
跨天段判据（昨天最后一站 → 今天第一站）**仍然会跑**，因为判据要保守（宁可漏报不误报）；
但它算出来的那个**下界**绝不进 `total_distance_km`。
→ 所以可能出现"判据说某天路程偏长，但总里程数字看起来很小"，**两者都对**。

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
raise RateLimited("操作太频繁", retry_after=42)   # -> 429 + Retry-After: 42 + detail.retry_after
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
3. 在 `api.md` 对应位置改样例 + 在 `DECISIONS.md` 记一条（**决定 / 替代方案 / 代价**，模板见该文件第七节）

> ⚠️ **反向操作是错的**：先在 `api.md` 改文字、`schemas.py` 忘了改 ——
> 两天后两份契约就开始不一致，而**不一致是查不出来的**（没有报错，只有前端"这个字段是 undefined"）。

---

## 八、这份契约还缺什么（诚实清单）

| 项 | 状态 |
|---|---|
| 幂等/重试的具体策略 | ✅ **已定**（1.5）：**只重试天然幂等的操作**，退避 1s→2s→4s 最多 3 次，`400/401/403` 不重试。**不引 `If-Match` 版本号** —— 单用户本地跑，多端编辑才需要 |
| SSE 断线恢复 | ✅ **契约已定**（1.5）：`id:` 帧 + `Last-Event-ID`，走 checkpoint 恢复不重发。**代码排 M6** |
| WebSocket vs SSE | ✅ SSE（单向够用）。**不需要双向**：用户消息走 POST，agent 消息走 SSE |
| 限流阈值 | ✅ **已定**（1.5 表 + `DECISIONS.md` D28）：按接口成本分层，最贵的 `chat` 10/小时·30/天，并发流同时 1 个。**M5 实现** |
| 文件上传的大小/类型限制 | ✅ **已定**（1.5）：**10MB**，`jpeg`/`png`/`webp`（**不收 SVG/GIF**），后端强制重编码为 512×512 WebP。**M9 实现** |
| 移动端 | ✅ **不做**（`DECISIONS.md` D30）：最佳 1440px / 最小 1024px，**只做桌面网页端**。窄屏加一层"请在电脑上打开"提示 |

**这份契约里唯一还没定的东西**：`AMAP_JS_KEY` 的真伪 —— **服务端验不了**（实测，
`DECISIONS.md` D24），只能到 M2 在浏览器里看报不报 `INVALID_USERKEY`。
