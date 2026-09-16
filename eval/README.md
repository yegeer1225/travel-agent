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
    --compare reports/<时间戳>-deepseek.json
```

## 报告里四个数字怎么看

| 指标 | 含义 | 看什么 |
|---|---|---|
| **pass@k** | ≥1 次成功的 case 占比 | 能力上限：最好情况能做多好 |
| **pass^k** | k 次全成功的 case 占比 | **一致性**：输出方差大它就掉（技术方案 风险 7） |
| **unknown 占比** | 判据里"无法判定"的比例 | 单列，不给通过率注水（方案.md 6.x） |
| **越界总数** | 软判据 msg 违反 D45 铁律的条数 | **门槛 = 0**，>0 就是软判据在编事实 |

## 口径（D53 定稿）

- **coldstart 成功** = 跑通 + 出行程 + **硬错 0** + expect 全满足（生成的行程不该带错）
- **hotstart 成功** = 跑通 + 出行程 + expect 全满足，**不要求硬错 0**
  ——粘贴行程的错误被 `should_flag` 抓到才是价值（抓错率）
- 判分**一行 LLM 都不碰**：硬判据读引擎算好的 checks，措辞用 `find_overreach` 正则，
  结构断言纯比较 —— 裁判不是被评的东西，数字才可信
- case 怎么写、什么错能标：见 `fixtures/README.md`
