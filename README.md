# 智能旅游规划系统

> 给一句话需求（或一段现成行程），产出一份**带坐标、开放时间、步行强度**的行程，
> 并自己检查哪里不对、改到能过为止。

全栈 AI 应用：**LangGraph 手写状态机 + FastAPI + React**。
agent 引擎是唯一主线，CRUD 只做"能接回主线"的部分。

---

## 先看长什么样

| 首页 · 一句话入口 | 景点页 · 收录库 + 城市分类 |
|---|---|
| ![首页](docs/screenshots/01-home.png) | ![景点页](docs/screenshots/02-spots.png) |

| 行程总览 · 逐站 + 距离 + 校验三态 | 攻略社区 · AI 行程一键转攻略 |
|---|---|
| ![行程总览](docs/screenshots/04-overview.png) | ![攻略社区](docs/screenshots/05-guides.png) |

| 个人中心 · 规划记录 / 收藏 | AI 路线规划助手 · 追问 → 生成 |
|---|---|
| ![个人中心](docs/screenshots/06-profile.png) | ![助手](docs/screenshots/03-assistant.png) |

> 截图来自**真实运行**（real 档高德数据，2026-09-20）。景点页底部「共收录 100 个景点」= 10 城 × 10 条收录库实况；
> 助手页可见核心行为：**缺必填信息先追问**（不硬编）→ 用户补一句 → 调 16 个工具生成行程。

---

## 一、它和"让大模型写一份行程"的区别

| 常见做法 | 这里的做法 |
|---|---|
| 模型凭记忆吐一份行程 | 模型**只能通过工具拿数据**（搜景点 / 查天气 / 测距）—— 景点真实存在、距离真实算过 |
| 行程对不对没人知道 | **9 类判据由代码判定**（5 硬 + 4 软），三态 `pass` / `fail` / `unknown` |
| 出错就整份重来 | **定位到"哪一天、哪一站、哪条判据"** → 带申诉表重生成，最多 2 轮 |
| 拿不到数据就编 | 拿不到标 `unknown` —— **不编，也不判错** |

**一句话说清定位**：这不是"会调大模型的增删改查"，是**一个有校验闭环的生成系统**。

---

## 二、两入口一内核

| 入口 | 输入 | 流程 |
|---|---|---|
| **冷启动** | 一句话需求（"后天去成都 2 天，带爸妈，节奏慢一点"） | 意图识别 → 生成 → 校验 → 定位问题 → 修正 → 再校验 |
| **热启动** | 粘贴一段现成行程 | 直接校验 → 定位问题 |

**内核只有一个**：`验证 → 定位问题 → 修正 → 再验证`。
两条入口共用同一套校验器 —— 这也是"出行程"之外，这个项目真正难的地方。

---

## 三、架构

```mermaid
flowchart TB
  subgraph G["LangGraph 手写状态机（内核）"]
    direction TB
    N1["意图识别"] --> N2["工具循环（上限 8 轮）"]
    N2 --> N3["生成行程"]
    N3 --> N4{"代码校验：9 类判据"}
    N4 -->|有问题| N5["定位到 天 / 站 / 判据<br/>带申诉表重生成（上限 2 轮）"]
    N5 --> N4
  end

  subgraph T["工具层 —— 数据的唯一来源"]
    direction LR
    T1["search_poi"] --- T2["get_weather"] --- T3["calc_distance"]
  end

  FE["前端：React 19 + Vite + Tailwind 4<br/>对话 / 路线总览 / 景点搜索 / 攻略社区 / 个人中心"]
  API["后端：FastAPI<br/>38 个端点 · SSE 流式 · JWT · 按接口成本分层限流"]
  OUT["行程输出：带坐标 · 开放时间 · 校验三态"]
  V["高德 Web 服务（real）<br/>内置 9 个真实 POI 池（mock）"]
  DB[("MySQL 8<br/>业务表 + langgraph-checkpoint")]

  FE --> API
  API --> N1
  N1 --> T
  T --> V
  N4 --> OUT
  G --> DB
```

**两个可切换的数据源**：`AMAP_PROVIDER=real|mock`（**默认 `real`**）。
real 走真·高德 Web 服务；mock 读内置的 9 个成都 POI（零配额、结果可复现，
用于单元测试 / 评测 / 没配 Key 时的兜底）。**两者接口与响应结构完全一致，前端无感知。**

⚠️ 填了 `real` 但没配 `AMAP_WEBSERVICE_KEY` 时**不会报错**，而是启动日志打警告 +
降级为 mock —— **当前生效档看 `GET /api/health`**，不要翻 `.env`。

---

## 四、技术栈

| 层 | 选型 |
|---|---|
| Agent | **LangGraph 1.2 手写 StateGraph**（不用 `create_agent` 封装）、LangChain Core |
| 模型 | DeepSeek / 通义千问（任何 OpenAI 兼容接口；按模型名自动路由 provider） |
| 后端 | FastAPI 0.141 + Pydantic 2.13 + uvicorn |
| 持久化 | MySQL 8 + `langgraph-checkpoint-mysql`（会话状态可恢复） |
| 鉴权 | JWT（PyJWT）+ bcrypt |
| 数据源 | 高德 Web 服务（自己写 3 个工具，不走 MCP） |
| 流式 | SSE（`astream_events` → `StreamingResponse`，每帧带 `id:`） |
| 前端 | React 19 + Vite + TypeScript + Tailwind 4 + Swiper |

---

## 五、跑起来

**前置**：Docker、Python 3.13、Node 22。

```bash
# 1) 配置：复制模板填值（高德 Key 可先不填 —— 会自动降级跑演示数据）
cp .env.example .env

# 2) 起数据库
docker compose up -d

# 3) 后端
python -m venv .venv
./.venv/Scripts/pip install -r requirements.txt          # Windows
cd backend && ../.venv/Scripts/python -m uvicorn app.api.main:app --reload

# 4) 前端（另开一个终端）
cd frontend && npm install && npm run dev                 # http://localhost:5173

# 5) 收录景点（**必做**，否则「旅游景点」页永远是空的）
cd backend && ../.venv/Scripts/python scripts/seed_spots.py \
    --city 成都 --keywords "宽窄巷子,武侯祠,杜甫草堂,锦里,大熊猫繁育研究基地"

# 6) 攻略社区的起始内容（**可选** —— 不跑就是空社区，功能不受影响）
cd backend && ../.venv/Scripts/python scripts/seed_official_guides.py
```

**为什么要第 5 步**：景点页搜的是**本站收录库**（`spots` 表），不是高德实时接口（D70）。
收录库是**真实高德 POI 的快照**，只能由这个脚本写入 —— **不手写、不编**（手写就是假数据）。
没跑过它，`/spots` 搜什么都返回空。收录过一次就长期有效（幂等，重跑即刷新）。

| 收录命令 | 作用 |
|---|---|
| `python scripts/seed_spots.py --city 成都 --keywords "..."` | 收录（每个关键词取 1 条，`--top N` 可调） |
| `python scripts/seed_spots.py --dry-run ...` | 只看会收什么，**不落库** |
| `python scripts/seed_spots.py --list` | 看当前收录了什么（含采集时间） |

**第 6 步（可选）为什么长这样**：攻略社区页的起始内容用的是**我们自己 agent 真实生成的行程**，
经渲染器变成攻略、挂在一个**官方号**下（`travel_platform` / 昵称「**旅游规划平台**」= 站名）。
**不用抓来的攻略** —— 那批是别人的文章（来源 URL 还"待补"），进社区等于转载且没署名（见 `eval/fixtures/README.md`）。
和收录景点一样，它是**开发期动作**、幂等可重跑（幂等键 `seed:<trip_id>`，重跑不会重复发）。

| 命令 | 作用 |
|---|---|
| `python scripts/seed_official_guides.py` | 发布起始攻略（默认最近 3 条真实行程，**按目的地去重**） |
| `python scripts/seed_official_guides.py --dry-run` | 只看会发什么，**不落库、不建号** |
| `python scripts/seed_official_guides.py --list` | 看官方号与它名下的攻略 |
| `python scripts/seed_official_guides.py --trip <trip_id>` | 指定某条行程（可重复传） |

> 依赖：库里得先有 `source='generated'` 的行程（即真的生成过几次）。一条都没有时脚本会提示。

两个 Key 说明：

- **只填 `LLM_API_KEY` 就能起后端** —— 没填高德 Key 时自动降级到演示数据（只有成都），启动日志有醒目警告
- 要真数据：填 `AMAP_WEBSERVICE_KEY`（服务平台选「Web 服务」）。**默认档就是 `real`**，不用改 `AMAP_PROVIDER`
- 想强制用演示数据（跑测试 / 省配额）：`AMAP_PROVIDER=mock`
- 前端地图另需 `AMAP_JS_KEY` + `AMAP_JS_SECURITY_CODE`（服务平台选「Web端(JS API)」）

**自检**：后端起来后开 `http://127.0.0.1:8000/verify` —— 一个零构建的冒烟页，把主要读路径和错误形状打一遍。

---

## 六、项目结构

```
backend/app/
  graph/          agent 内核：意图 / 工具循环 / 生成 / 校验 / 修正 / 粘贴路径
  tools/          3 个工具（search_poi / get_weather / calc_distance）
  providers/      数据源：amap（真）/ mock（9 点池）—— 同签名可切换
  api/routes/     38 个端点，按资源分文件
  schemas.py      ★ 数据契约唯一事实源（70 个导出名）
  store/          MySQL 仓储
  services/       攻略渲染（guide_render.py）—— 限流在 api/ratelimit.py
backend/tests/    612 个用例（`pytest tests/ -q`，2026-09-19 实测全绿）
eval/             评测集（13 条 case + runner + 报告）
docs/api.md       接口契约（38 个端点 / 10 种 SSE 事件 / 错误码表）
docs/types.ts     67 个 TS 类型 —— 由 schemas.py 自动生成，前端不手抄
DECISIONS.md      ★ 81 条决策记录：为什么这么定 / 没选什么 / 代价 / 重审触发
方案.md            做什么         技术方案.md  怎么实现        界面设计.md  长什么样
```

---

## 七、几个工程点

放在这里是因为它们**有代价、有取舍**，不是"用了什么库"这种描述。完整版在 `DECISIONS.md`。

- **数据契约唯一事实源**——`schemas.py` 是唯一的类型定义处，`docs/types.ts` 由脚本自动生成。
  改了契约必须重跑代码生成 + 过 `tsc --strict`。手抄类型两天后必然不一致，**而且不一致不报错**（只是某字段是 `undefined`）。
- **工具的"零幻觉"设计**——模型不能凭记忆说景点在哪儿，坐标/开放时间/距离全部来自工具返回；
  拿不到就标 `unknown`，**既不编也不判错**。
- **判据假阳性比假阴性贵**——把一份正常行程误判成错误，会触发无意义的重生成（实测踩过：距离模型用错常量，白打回两轮）。
  所以每条阈值判据都配"**界内绿 + 超一点红**"两个测试。
- **越权的结构性防线**——不靠"记得写 `WHERE user_id`"（那是约定不是结构，违反不报错），
  而是签名级约束 + 三个扫描断言（写错就测试红）。
- **只重试天然幂等的操作**——`GET` / 搜索 / 天气 / 距离 / LLM 可以退避重试；
  改状态的操作靠唯一索引兜底，**绝不重试**。
- **限流按接口成本分层**——最贵的对话接口 10 次/小时，搜索 120 次/分钟。
- **三态校验而不是布尔**——`unknown` 是独立状态：数据没拿到时如实标注，不当通过也不打回。

---

## 八、评测

`eval/` 下 13 条 case，覆盖 3 类判据的**边界对**（界内绿 ↔ 超一点红）。

定位是 **regression set（回归防线），不是 benchmark** —— 它的用处是回答"改 prompt / 换模型之后，
系统是变好还是变差"，不是"这个 agent 有多强"。

```bash
./.venv/Scripts/python.exe eval/run_eval.py --label my-run        # 全量
./.venv/Scripts/python.exe eval/run_eval.py --cases "*green*.json" # 只跑部分
```

两条刻意的设计取舍：

- **答案由人给，不由 AI 给** —— 人出题（含埋点与期望）、引擎答题、**代码阅卷（零 LLM）**，
  三方互不知道对方，只在报告比对处相遇。否则就是自评闭环。
- **可以断言"不该报警"** —— 只有"必须报"的正向断言，误报率就无处安放。

---

## 九、当前边界

诚实地列出来，比装作没有强：

| 项 | 现状 |
|---|---|
| 端 | **只做桌面端**：最佳 1440px、最小 1024px，不做响应式（`<768px` 显示提示层） |
| **数据源档位** | 两档，前端无感知（D23）：**`real` = 默认档**（真高德，任何城市都行，2026-09-17 实测成都 / 杭州双城正常，含距离与天气）；`mock` = 内置 9 个成都 POI（测试 / 评测 / 无凭据兜底）。⚠️ 配 `real` 但缺 `AMAP_WEBSERVICE_KEY` 时**不报错**，而是启动日志警告 + 降级 mock —— **当前生效档看 `GET /api/health` 的 `mock_mode` / `amap_degraded`**，别翻 `.env`（它只代表"下次启动用什么"） |
| **单次生成耗时** | 实测 **~235s（3 分 55 秒）**：北京 5 天重需求，real 档端到端（2026-09-18，P1 架构实测，qwen3.7-flash 档）。P1 改造前同需求 **554~666s**（9m14s~11.1min）—— 把搜索子 agent 的"多轮出关键词"决策循环（12~20 次 LLM 调用）改成主 agent 首轮一次性批量派发（D79）后砍掉近 6 成。改造前旧架构实测区间 146~371s（3 天轻需求），瓶颈从来不是数据接口（高德搜索全程仅 ~15s），是 LLM 调用次数。DeepSeek 付费档再快一个量级（~40-70s 量级，估算待实测） |
| 门票 | **不做价格筛选** —— 高德该字段大面积缺失，硬做就是编 |
| 天气 | 只有未来 **4 天**（高德接口上限，从今天算起）。⚠️ **演示必须选未来 4 天内的日期** —— 超窗时三天的 `weather.status` 全是 `unavailable`、`weather_conflict` 判据等于白跑。行为本身是正确的（拿不到就标 `unknown`，绝不编） |
| 高德限流 | 有 QPS 限制 → 出站加 0.45s 最小间隔（≈2.2 QPS） |
| SSE 断线恢复 | **前后端都已实现**：后端每帧带 `id:`、认 `Last-Event-ID` 走 checkpoint 恢复；前端自动重连（退避 1.5/3/6s，上限 3 次，2026-09-19 补齐） |
| 登出 | 无 `POST /auth/logout`（JWT 无状态，前端删 token 即登出）—— 代价是已签发 token 在 `exp` 前仍有效 |
| 部署 | 数据库容器化，**应用本身不容器化**（本地开发改代码不用重建镜像） |

---

## 十、文档地图

这是一个"文档先于代码"的项目，几个文件的分工是刻意的：

| 文件 | 只回答 |
|---|---|
| `AGENTS.md` | **AI 协作入口**：项目一句话 / 目录地图 / 铁律速查 —— 任何 AI 进仓库干活先读这页 |
| `方案.md` | **做什么**（含 A1~A44 决策与用户确认原话） |
| `技术方案.md` | **怎么实现** |
| `界面设计.md` | **长什么样**（设计 token / 视觉规范） |
| `DECISIONS.md` | **为什么这么定 / 没选什么 / 代价 / 什么情况下要重审** |
| `docs/api.md` | 接口契约（已冻结） |
| `docs/types.ts` | 前端类型（自动生成，不手抄） |
| `eval/README.md` | 评测怎么跑、case 怎么写 |
| `前端交接.md` / `后端交接.md` | 前后端协作交接清单与验收记录 |

---

## 十一、许可

[MIT License](LICENSE) © 2026 yegeer —— 可自由使用、修改、分发（含商用），保留版权声明即可。
