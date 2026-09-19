# AGENTS.md — AI 协作入口

> 任何 AI（WorkBuddy / 豆包 / Claude Code / 其他 agent）在这个仓库干活前，**先读完这一页**。
> 它只做三件事：项目是什么、东西在哪、规矩是什么。细节一律跳转对应文档，**本页不转载事实**——
> 转载必腐烂（代码与文档漂移的学费已付过，见 DECISIONS.md）。

---

## 1. 项目一句话

**智能旅游规划系统**（实习求职作品）：给一句话需求（或粘贴一段现成行程），产出
**带坐标、开放时间、步行强度**的结构化行程，并用**代码校验闭环**（验证 → 定位问题 → 修正 → 再验证）
保证"不是模型编的"。

全栈 AI 应用：**LangGraph 手写 StateGraph + FastAPI + React 19.3**。
agent 引擎是唯一主线，CRUD 只做"能接回主线"的部分——这不是"会调大模型的增删改查"，
是**一个有校验闭环的生成系统**。详细定位读 `README.md`，需求边界读 `方案.md`。

## 2. 技术栈与档位

| 层 | 用什么 | 备注 |
|---|---|---|
| agent 引擎 | LangGraph 手写 `StateGraph`（**不用** `create_react_agent` 等预置抽象） | 决策见 DECISIONS.md D 系列 |
| 后端 | FastAPI + Pydantic + PyMySQL | `backend/app/` |
| 前端 | React 19.3 + Vite 8 + TypeScript + Tailwind 4（无 config） | `frontend/`，**实现由豆包负责** |
| 模型 | 默认档 = 百炼 `qwen3.7-plus`；面试演示专款 = DeepSeek 官方（余额冻结） | `.env` 切换；**挡位是进程级**，起服务后只看 `/api/health`；改 `.env` 必须重启 |
| 存储 | MySQL 8（`docker-compose.yml` 只起库，应用不容器化） | 库连接不上**先查 Docker Desktop 是否启动** |
| 评测 | `eval/run_eval.py` + `eval/fixtures/`（**只读**，评测集就是契约的一部分） | 评测烧 token，跑之前先算额度账（D81 教训） |

## 3. 目录地图

| 路径 | 一句话 |
|---|---|
| `backend/app/graph/` | agent 内核：`nodes.py`（节点+系统提示词）、`state.py`（图状态）、`graph.py`（装配+运行时） |
| `backend/app/api/` | HTTP 层：`routes/`（按域分文件）、`chat_stream.py`（SSE）、`main.py`（create_app 工厂+依赖注入） |
| `backend/app/store/` | 持久层：`repo.py`（SQL repo）、`memory.py`（测试用内存替身，**排序规则必须与 SQL 逐条对齐**）、`db.py`（连接） |
| `backend/app/providers/` | 数据源：高德 real / mock（`base.py` 是协议）；读路径已全面收录库化，provider 只剩详情回落 |
| `backend/app/schemas.py` | 🔴 **契约唯一事实源**（所有对外模型） |
| `backend/scripts/` | `seed_spots.py`（收录库唯一写入路径）、`gen_ts_types.py`（契约→TS）、`run_cli.py`（命令行跑图）等 |
| `backend/tests/` | pytest 全量；fakes 与生产同签名、不连库 |
| `frontend/src/` | `pages/`（页面）、`components/`、`lib/api.ts`（唯一出口）、`types/contract.ts`（🔴 自动生成勿手改） |
| `docs/` | `api.md`（对外契约）、`types.ts`（生成产物）、`LLM修复交接-*.md` |
| `eval/` | 评测脚本 + fixtures（只读）+ 历史结果 |

## 4. 怎么跑

```bash
# 库（MySQL 只能用户手动启动 Docker Desktop，AI 勿自动拉起）
docker compose up -d            # 等 healthy 再连

# 后端（8000）
cd backend && ../.venv/Scripts/python.exe -m uvicorn app.api.main:app --port 8000
# ⚠️ 改任何 .py / .env 必须重启进程

# 前端（vite dev，/api 代理到 127.0.0.1:8000）
cd frontend && npm run dev

# 测试（改动后必须全绿才算完成）
cd backend && ../.venv/Scripts/python.exe -m pytest -q
cd frontend && npx tsc --noEmit && npm run lint
```

**改契约三步走**（漏一步 = TS 编译期不报错、运行时静默错位）：
1. 改 `backend/app/schemas.py` → 2. `cd backend && ../.venv/Scripts/python.exe scripts/gen_ts_types.py`
并同步 `docs/types.ts` → `frontend/src/types/contract.ts` → 3. 前端 `tsc --noEmit` 验证 + 更新 `docs/api.md`。

**收录景点**：`backend/scripts/seed_spots.py`（强制 real provider、幂等、记 `source`）——
🔴 **绝不手写 INSERT**，手写 = 编数据（D20 教训）。

## 5. 铁律速查（每条的完整理由在指向处，别在这里现场发明新规矩）

| 铁律 | 指向 |
|---|---|
| 契约三件套：`schemas.py` → `api.md` → `types.ts`/`contract.ts`，**同一事实只写一处** | 本页第 4 节 |
| 收录库搜索**空列表是正确行为，不回落**；详情才回落（按 id ≠ 搜索） | `api.md` 3.5 / D70 |
| 高德三坑：HTTP 200 却限流（10021）**必须退避重试**；`cost/open_time/rating` 是 `str \| []`（空数组→None 不当 0）；出站 0.45s 限速 + 状态/缓存模块级 | `providers/` 注释 |
| 工具上限：总 ≤20 / L2 ≤8 / L3 ≤2（D25） | DECISIONS.md D25 |
| 测试纪律：fakes 与生产同签名、不连库、模块级状态 autouse 清理 | `tests/conftest.py` |
| 文档分工：`方案.md`=是什么 / `DECISIONS.md`=为什么·代价·重审 / `技术方案.md`=怎么做 | 三份文档头部 |
| Python 代码/注释里中文引号用『』（避免和字符串引号打架） | 现有代码风格 |
| 🔴 没有对照组的观察不是判据；怀疑文档写错先实测再讲 | DECISIONS.md D80 |

## 6. 文档索引（什么时候读哪份）

| 文档 | 用途 | 什么时候读 |
|---|---|---|
| `README.md` | 项目门面：定位、架构图、两入口一内核 | 第一次了解项目 |
| `方案.md` | 需求与功能边界（A 面） | 判断"该不该做某功能" |
| `技术方案.md` | 模块设计与数据流（怎么做） | 动后端结构前 |
| `DECISIONS.md` | 全部决策记录：为什么、代价、重审触发（D1~D81+） | 🔴 每次想"推翻/重议"某个行为前必读 |
| `docs/api.md` | 对外契约：接口、错误码、限流、SSE 事件表 | 接口对接/改契约 |
| `前端交接.md` | 给豆包的逐节交接（按节追加，含验收清单） | 前端干活前 |
| `后端交接.md` | 豆包转交给后端的需求（R1 起） | 后端接活前 |
| `面试材料-*.md` | 简历讲解稿 + 追问预案 | 求职准备 |

## 7. 当前状态（改完代码顺手更新这一行）

后端 **612 测试全绿**；模块 M0~M12 全关闭；性能收口（同需求 9m14s→3m55s，D76~D82）。
「脱离 demo 级别」完善：后端侧已收口 —— README 数字修正、CI（`.github/workflows/ci.yml`）、
日志落盘（D82）、`GET /spots` 列表 + `/home` 收录库化（后端交接 R1）；
前端侧健壮性清单在 `前端交接.md` 第二十节（ErrorBoundary / SSE 重连 / 404 / typecode 映射，交豆包）。
协作流：叶鸽儿拍板 → 后端 AI / 前端豆包按交接文档执行 → 验收清单逐条过。
