# 评测集录入指南（fixtures）

这里放**人工筛过的真实攻略原文**（10~20 篇起步，先攒成都的）。
评测运行器只读 `eval/cases/*.json`；fixtures 是**原文素材**，
case 从这里生成（原文 + 你标注的已知错误）。

> ⚠️ **单向同源（A38）**：`fixtures → 产品`（可当攻略库种子数据），
> **永不回读**——线上用户发的攻略不自动进评测集。

## 攻略怎么攒（技术方案.md 第 8 节的原始任务）

1. 浏览器打开马蜂窝游记 / 知乎 / 公众号里的**含具体行程的攻略**
   （要有 Day1/Day2…、交通方式、时间）
2. 全文复制存成 `.md`，文件名 = `来源-城市-标题缩写.md`（如 `mafengwo-chengdu-2d.md`）
3. 每篇记一笔你知道的**问题**（下一节讲什么能标）

## 什么错能标进评测（风险 11 的纪律）

**只标硬判据能验证的错** —— 判分零 LLM，标了判不了的都是白标：

| 能标 ✅ | 例子 | 对应判据 |
|---|---|---|
| 一天排太多站 | "带爸妈一天 5 个点" | `walk_load`（步行估算超上限） |
| 一天车程太长 | "这天三个点相距 40km" | `reachable`（单跳 >120min / 当天累计 >180min） |
| 不存在的地点 | 名字搜不到的店 | `poi_exists` |
| 日期/营业时间冲突 | "周一看博物馆" | `open_today` |
| 结构性超量 | "7 天行程标成 3 天" | `max_days` / `min_stops` |

| 不能标 ❌ | 为什么 |
|---|---|
| "这个行程不合理" / "太赶了"（无数字） | 主观，硬判据不产生对应 fail |
| "门票写错了" | 门票数据大面积缺失，判不了（坑 4） |
| "这人少/人多" | 没有数据源 |

## 从攻略原文到 case

原文攒好后，每个 case 一个 JSON 放 `eval/cases/`（格式照着 `demo-*.json` 抄）：

```json
{
  "id": "mafengwo-chengdu-2d",
  "kind": "hotstart",
  "title": "马蜂窝某篇成都两日（含 2 处标注错误）",
  "source": "马蜂窝 <URL>",
  "text": "攻略原文的行程部分（Day1…Day2…，别整篇博客都贴进来）",
  "destination": "成都",
  "k": 3,
  "expect": {
    "should_flag": [
      { "code": "walk_load", "day": 2, "note": "原文带爸妈却排了 5 个点" }
    ],
    "must_contain_pois": ["武侯祠"]
  }
}
```

- `code` 只能取硬判据的：`poi_exists` / `open_today` / `reachable` / `walk_load`
  （⚠️ 当天车程超限的 code 也是 `reachable`，看 msg 区分）
- `k ≥ 3` 才有 pass^k 的意义（一致性）；k 越大越贵，DeepSeek 一次全流程约 0.3~0.5 元
- 冷启动 case（`kind: "coldstart"`）测的是**生成**：文本写一句话需求，
  `expect` 用 `max_days` / `min_stops`，不标 `should_flag`
