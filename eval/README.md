# M8 评测

**回答一个问题：改一行 prompt（或换一个模型），系统变好了还是变差了？**

```
eval/
├── run_eval.py        运行器（判分零 LLM，全代码可算）
├── cases/             评测 case（每个一个 JSON；demo-* 是演示，非真实攻略）
├── fixtures/          真实攻略原文（人工筛，录入方法见 fixtures/README.md）
└── reports/           报告输出（.md 给人看 / .json 给 --compare）
```

## 怎么跑

```bash
cd /d/vscode\ Projiects/agent   # 项目根

# 1. 快速自检（跟 .env 的 provider，现在是 mock，秒级）
./.venv/Scripts/python.exe eval/run_eval.py

# 2. 出真报告数字（真实高德 + 真 LLM，慢是正常的：高德 0.45s/次限速）
./.venv/Scripts/python.exe eval/run_eval.py --provider real --label deepseek

# 3. 双模型对比：改 .env 的 LLM_MODEL_TOOL / LLM_MODEL_PLAN → 再跑一次 → 对比
./.venv/Scripts/python.exe eval/run_eval.py --provider real --label qwen \
    --compare eval/reports/<时间戳>-deepseek.json
```

## ⚠️ `--provider mock` 省高德的钱，**不省 LLM 的钱**

容易踩的认知陷阱（2026-09-17 实测确认）：

| | `--provider mock` | `--provider real` |
|---|---|---|
| 高德出站 | ❌ 零调用（读本地 9 个点的池） | ✅ 真调用（0.45s/次限速） |
| **LLM** | ✅ **照常真调用** | ✅ 真调用 |

`--provider` 只管**数据源**；LLM 永远走 `.env` 的 `LLM_MODEL_TOOL` / `LLM_MODEL_PLAN`。
所以"mock 模式下跑评测不要钱"是错的 —— 每条 case 都在真调模型：

| 路径 | 每条 case 的 LLM 次数 | 实测耗时 |
|---|---|---|
| hotstart | 2（解析地名清单 + 软检查） | 3s 左右 |
| coldstart | 整个生成循环 | 27~29s |

**想让评测跑在免费额度上**：把 `.env` 两个模型换成百炼档（`qwen3.7-flash`）。
代价是慢 —— 同类 prompt 实测 51.8s vs `deepseek-flash` 3.7s（差 14 倍），
hotstart 一条就从 3s 涨到 100s+。要跑全量建议留给睡前。

## 报告里四个数字怎么看

| 指标 | 含义 | 看什么 |
|---|---|---|
| **pass@k** | ≥1 次成功的 case 占比 | 能力上限：最好情况能做多好 |
| **pass^k** | k 次全成功的 case 占比 | **一致性**：输出方差大它就掉（技术方案 风险 7） |
| **unknown 占比** | 判据里"无法判定"的比例 | 单列，不给通过率注水（内部需求文档 6.x） |
| **越界总数** | 软判据 msg 违反 D45 铁律的条数 | **门槛 = 0**，>0 就是软判据在编事实 |

## 口径（D53 定稿）

- **coldstart 成功** = 跑通 + 出行程 + **硬错 0** + expect 全满足（生成的行程不该带错）
- **hotstart 成功** = 跑通 + 出行程 + expect 全满足，**不要求硬错 0**
  ——粘贴行程的错误被 `should_flag` 抓到才是价值（抓错率）
- **正例 / 阈值边界例** 靠 `expect.should_not_flag` 表达"不该报"：命中即判失败（**误报**）。
  `status=unknown` 不算误报（三态）。没有它，所有 case 都只能写成"必须命中"
- 判分**一行 LLM 都不碰**：硬判据读引擎算好的 checks，措辞用 `find_overreach` 正则，
  结构断言纯比较 —— 裁判不是被评的东西，数字才可信
- case 怎么写、什么错能标：见 `fixtures/README.md`
