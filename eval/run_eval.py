"""M8 评测运行器：读 case → 跑 agent → 判分 → 报告（pass@k / pass^k / unknown 占比）。

═══════════════════════════════════════════════════════════════
 为什么判分**一行 LLM 都不碰**
═══════════════════════════════════════════════════════════════

评测的可信度来自「裁判不是被评的那个东西」。被评的是 agent（LLM 驱动），
判分就必须全是**确定性代码**：硬判据结果读 trip 里的 checks（引擎算的），
措辞合规用 `find_overreach`（正则），结构断言是纯比较。
否则"评测说它变好了"永远可以被追问一句——"会不会是评测模型自己变了"。

```bash
# 全部 case，按 case 里写的 k 跑（provider 跟 .env，现在 = mock）
../.venv/Scripts/python.exe eval/run_eval.py

# 真实数据源评测（高德 Web 服务 Key + 真 LLM）——这才是出报告数字的跑法
../.venv/Scripts/python.exe eval/run_eval.py --provider real --label deepseek

# 双模型对比：换模型再跑一次同 label 口径，然后并排比
# （改 .env 的 LLM_MODEL_TOOL / LLM_MODEL_PLAN → 跑 → --compare）
../.venv/Scripts/python.exe eval/run_eval.py --provider real --label qwen --compare reports/<stamp>-deepseek.json
```

═══════════════════════════════════════════════════════════════
 判分口径（D53 定稿，与 方案.md 6.x 的评测口径对接）
═══════════════════════════════════════════════════════════════

| 项 | 评法 | 来源 |
|---|---|---|
| coldstart 成功 | 跑通 + 出行程 + **硬错 0** + expect 全满足 | 生成是"应该没错"的路径，打回机制兜着 |
| hotstart 成功 | 跑通 + 出行程 + expect 全满足（**不要求硬错 0**） | 粘贴行程自带错误，`should_flag` 抓到才算本事 |
| `should_flag` | flatten 后存在 code 相同 + status=fail + day 匹配的判据 | 攻略标注的"已知错"抓错率（技术方案 风险 11：只标硬判据可验证的错） |
| `must_contain_pois` | 行程站名含该子串 | hotstart 保真度：粘贴的站不许被解析丢掉 |
| `max_days` / `min_stops` | 结构断言 | 需求遵循度 |
| 软判据越界 | `find_overreach(msg)` 非空的条数 | **门槛 = 0**（D45：不能评对错就评越界） |
| unknown 占比 | 全部 checks 里 unknown 的比例 | **报告单列**，不给 pass 率注水（方案.md 6.x） |

pass@k（case 级聚合）= k 次里**至少 1 次**成功的 case 占比；
pass^k = **全部成功**的 case 占比（一致性，对冲输出方差 —— 技术方案 风险 7）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date as Date
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.graph.graph import (  # noqa: E402
    RECURSION_LIMIT,
    build_graph,
    build_runtime,
    initial_state,
)
from app.graph.soft import find_overreach  # noqa: E402
from app.schemas import SSEEventType  # noqa: E402

CASES_DIR = ROOT / "eval" / "cases"
REPORTS_DIR = ROOT / "eval" / "reports"

_LINE = "─" * 72


# ══════════════════════════════════════════════════════════════
#  判分（零 LLM）
# ══════════════════════════════════════════════════════════════


def _flatten_where(trip: dict) -> list[tuple[str, dict]]:
    """把三层 checks 收拢成 `(位置, check)` —— 位置形如 `d2` / `d2s3` / `trip`。

    不复用 `app.graph.validate.flatten_checks`：那里为了喂前端把**位置丢了**，
    而 `should_flag` 要按 day 精确匹配（"第 2 天车程爆" ≠ "任意一天爆"）。
    """
    out: list[tuple[str, dict]] = []
    for day in trip.get("days") or []:
        where_d = f"d{day['day']}"
        out.extend((where_d, c) for c in day.get("checks") or [])
        for stop in day.get("stops") or []:
            out.extend((f"{where_d}s{stop['seq']}", c) for c in stop.get("checks") or [])
    out.extend(("trip", c) for c in trip.get("checks") or [])
    return out


def _expect_failures(trip: dict, expect: dict) -> list[str]:
    """返回 expect 里**没满足**的项（空列表 = 全满足）。"""
    bad: list[str] = []
    where_checks = _flatten_where(trip)
    stops = [s for d in trip.get("days") or [] for s in d.get("stops") or []]

    for want in expect.get("should_flag") or []:
        code, day = want.get("code"), want.get("day")
        hit = any(
            c.get("code") == code and c.get("status") == "fail"
            and (day is None or w == f"d{day}" or w.startswith(f"d{day}s"))
            for w, c in where_checks
        )
        if not hit:
            bad.append(f"should_flag 未触发：{code}" + (f"（day {day}）" if day else ""))

    for name in expect.get("must_contain_pois") or []:
        if not any(name in (s.get("name") or "") for s in stops):
            bad.append(f"must_contain_pois 丢了：{name}")

    if (max_days := expect.get("max_days")) is not None and len(trip.get("days") or []) > max_days:
        bad.append(f"max_days 超了：{len(trip.get('days') or [])} > {max_days}")
    if (min_stops := expect.get("min_stops")) is not None and len(stops) < min_stops:
        bad.append(f"min_stops 没到：{len(stops)} < {min_stops}")
    return bad


def _grade_run(trip: dict | None, err: str | None, case: dict) -> dict:
    """一次运行 → 判分结果。**只看代码可算的东西**。"""
    expect = case.get("expect") or {}
    result: dict = {"ok": False, "reasons": [], "unknown": None, "overreach": None,
                    "hard_errors": None, "stop_count": None}

    if err or trip is None:
        result["reasons"] = [err or "没有产出行程"]
        return result
    if hasattr(trip, "model_dump"):
        trip = trip.model_dump(mode="json")

    hard_errors = trip.get("summary", {}).get("hard_errors")
    expect_bad = _expect_failures(trip, expect)

    # coldstart 生成路径**必须**硬错 0（打回机制兜底后的产物还带硬错 = 引擎失职）；
    # hotstart 恰恰相反 —— 粘贴行程的错误被抓出来才是价值，看 should_flag。
    if case["kind"] == "coldstart" and hard_errors:
        expect_bad.append(f"生成行程带硬错 {hard_errors}（应为 0）")

    where_checks = _flatten_where(trip)
    unknown = [c for _, c in where_checks if c.get("status") == "unknown"]
    soft_msgs = [c.get("msg") for _, c in where_checks if c.get("level") == "soft"]
    overreach = [hits for m in soft_msgs if (hits := find_overreach(m))]

    result.update(
        ok=not expect_bad,
        reasons=expect_bad,
        unknown=len(unknown),
        unknown_total=len(where_checks),
        overreach=sum(len(h) for h in overreach),
        hard_errors=hard_errors,
        stop_count=len(stops := [s for d in trip.get("days") or [] for s in d.get("stops") or []]),
    )
    return result


# ══════════════════════════════════════════════════════════════
#  跑一次 / 跑一个 case
# ══════════════════════════════════════════════════════════════


async def _run_hotstart(case: dict, provider) -> tuple[object | None, str | None]:
    """粘贴校验路径：直接吃 `paste_trip_stream` 的事件，取 Trip / Error。"""
    from app.graph.paste import paste_trip_stream

    nodes = build_runtime(provider=provider)
    trip, err = None, None
    async for item in paste_trip_stream(
        nodes, text=case["text"], destination=case.get("destination"), user_id=0,
    ):
        if item.get("heartbeat"):
            continue
        evt = item["event"]
        if evt.type == SSEEventType.TRIP:
            trip = evt.trip
        elif evt.type == SSEEventType.ERROR:
            err = f"{evt.code}: {evt.msg}"
    return trip, err


async def _run_coldstart(case: dict, provider) -> tuple[object | None, str | None]:
    """一句话生成路径：run_cli 同款 —— `build_graph.ainvoke`，不碰 HTTP。"""
    from app.graph.graph import build_graph as _bg  # noqa: F401（保持 import 集中在一处）

    today = Date.fromisoformat(case["today"]) if case.get("today") else None
    nodes = build_runtime(provider=provider, today=today)
    graph = build_graph(nodes)
    state = await graph.ainvoke(
        initial_state(case["text"]), {"recursion_limit": RECURSION_LIMIT},
    )
    trip, err = state.get("trip"), state.get("error")
    if trip is None and not err and state.get("ask"):
        err = "图停在追问（需求缺关键字段：目的地/日期）—— case 文本信息不全"
    return trip, err


async def run_case(case: dict, *, provider, k: int, quiet: bool) -> dict:
    """一个 case 跑 k 次，逐次判分。"""
    runs = []
    for i in range(k):
        t0 = time.monotonic()
        if case["kind"] == "hotstart":
            trip, err = await _run_hotstart(case, provider)
        else:
            trip, err = await _run_coldstart(case, provider)
        elapsed = round(time.monotonic() - t0, 1)
        grade = _grade_run(trip, err, case)
        grade["run"] = i + 1
        grade["elapsed_s"] = elapsed
        runs.append(grade)
        if not quiet:
            mark = "✅" if grade["ok"] else "❌"
            tail = "" if grade["ok"] else "｜" + "；".join(grade["reasons"][:2])
            extra = ""
            if grade.get("unknown") is not None:
                extra = f"｜unknown {grade['unknown']}/{grade['unknown_total']}｜越界 {grade['overreach']}"
            print(f"    {mark} 第 {i + 1}/{k} 次（{elapsed}s）{extra}{tail}")

    passed = sum(1 for r in runs if r["ok"])
    return {
        "id": case["id"], "kind": case["kind"], "title": case.get("title", ""),
        "k": k, "passed": passed,
        "pass_at_k": 1 if passed else 0,          # 单 case：≥1 次成功 → 1
        "pass_pow_k": 1 if passed == k else 0,    # 单 case：全成功 → 1
        "unknown_pct": _pct([r for r in runs if r.get("unknown") is not None],
                            lambda r: (r["unknown"], r["unknown_total"])),
        "overreach_total": sum(r["overreach"] or 0 for r in runs),
        "runs": runs,
    }


def _pct(runs: list[dict], get) -> float | None:
    """unknown 占比 = Σunknown / Σtotal。没有可算的 run → None（不注水成 0%）。"""
    u = sum(get(r)[0] for r in runs)
    t = sum(get(r)[1] for r in runs)
    return round(u / t * 100, 1) if t else None


# ══════════════════════════════════════════════════════════════
#  报告
# ══════════════════════════════════════════════════════════════


def _aggregate(results: list[dict]) -> dict:
    n = len(results) or 1
    unknowns = [r["unknown_pct"] for r in results if r.get("unknown_pct") is not None]
    return {
        "cases": len(results),
        "pass_at_k": round(sum(r["pass_at_k"] for r in results) / n * 100, 1),
        "pass_pow_k": round(sum(r["pass_pow_k"] for r in results) / n * 100, 1),
        "unknown_pct_avg": round(sum(unknowns) / len(unknowns), 1) if unknowns else None,
        "overreach_total": sum(r["overreach_total"] for r in results),
        "provider": None,  # 由调用方填
        "label": None,
    }


def _write_report(results: list[dict], agg: dict, label: str) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS_DIR / f"{stamp}-{label}.json"
    md_path = REPORTS_DIR / f"{stamp}-{label}.md"

    json_path.write_text(
        json.dumps({"aggregate": agg, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        f"# 评测报告：{label}", "",
        f"- 时间：{stamp}｜provider：`{agg['provider']}`｜cases：{agg['cases']}",
        f"- **pass@k = {agg['pass_at_k']}%**（≥1 次成功的 case 占比）",
        f"- **pass^k = {agg['pass_pow_k']}%**（k 次全成功的 case 占比，一致性）",
        f"- unknown 占比均值：{agg['unknown_pct_avg']}%　｜软判据越界总数：**{agg['overreach_total']}**（门槛 0）",
        "",
        "| case | kind | k | 成功 | pass@k | pass^k | unknown% | 越界 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} | {r['kind']} | {r['k']} | {r['passed']}/{r['k']} "
            f"| {r['pass_at_k']} | {r['pass_pow_k']} | {r['unknown_pct']} | {r['overreach_total']} |"
        )
    lines += ["", "## 失败明细", ""]
    fails = [(r, run) for r in results for run in r["runs"] if not run["ok"]]
    if not fails:
        lines.append("（无）")
    for r, run in fails:
        lines.append(f"- `{r['id']}` 第 {run['run']} 次（{run['elapsed_s']}s）："
                     + "；".join(run["reasons"]))
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, json_path


def _compare(mine_json: Path, other_json: Path) -> str:
    """两份报告 JSON 的聚合对比 —— 双模型对比的出表方式。"""
    a = json.loads(mine_json.read_text(encoding="utf-8"))["aggregate"]
    b = json.loads(other_json.read_text(encoding="utf-8"))["aggregate"]
    rows = ["| 指标 | 本次 | 对比 | Δ |", "|---|---|---|---|"]
    for key, title in [("pass_at_k", "pass@k %"), ("pass_pow_k", "pass^k %"),
                       ("unknown_pct_avg", "unknown 占比 %"), ("overreach_total", "越界总数")]:
        va, vb = a.get(key), b.get(key)
        delta = (round(va - vb, 1) if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else "—")
        rows.append(f"| {title} | {va} | {vb} | {delta} |")
    rows += ["", f"对比基准：`{other_json.name}`（label={b.get('label')}，provider={b.get('provider')}）"]
    return "\n".join(rows)


# ══════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════


def _load_cases(pattern: str) -> list[dict]:
    paths = sorted(CASES_DIR.glob(pattern))
    cases = []
    for p in paths:
        case = json.loads(p.read_text(encoding="utf-8"))
        case.setdefault("k", 1)
        if case.get("expect") is None:
            case["expect"] = {}
        cases.append(case)
    return cases


async def main() -> int:
    parser = argparse.ArgumentParser(description="M8 评测运行器（判分零 LLM）")
    parser.add_argument("--cases", default="*.json", help="case 文件 glob（默认全部）")
    parser.add_argument("--k", type=int, help="覆盖 case 里的 k")
    parser.add_argument("--provider", choices=["mock", "real"],
                        help="覆盖 .env 的 AMAP_PROVIDER（real = 高德 Web 服务 + 限速）")
    parser.add_argument("--label", default="run", help="报告文件名标签（双模型对比用）")
    parser.add_argument("--compare", type=Path, help="对比另一份报告 JSON，出并排表")
    parser.add_argument("--quiet", action="store_true", help="不逐次打印")
    args = parser.parse_args()

    cases = _load_cases(args.cases)
    if not cases:
        print(f"没有找到 case：{CASES_DIR / args.cases}")
        return 1

    provider = None
    provider_name = "mock" if settings.mock_mode else "real"
    if args.provider == "real" and settings.mock_mode:
        # 显式覆盖：不读 .env 的 mock 开关，直接构造真实 provider（Key 必须已配）
        from app.providers.amap import AmapHttpProvider

        provider = AmapHttpProvider(settings.amap_webservice_key)
        provider_name = "real"
        print(f"⚠️ --provider real：真实高德出站（限速 {provider._min_interval_s}s/次），评测会慢是正常的")
    elif args.provider == "mock":
        from app.providers.mock import MockAmapProvider

        provider = MockAmapProvider()
        provider_name = "mock"

    print(f"{_LINE}\n M8 评测｜label={args.label}｜provider={provider_name}"
          f"｜cases={len(cases)}\n{_LINE}")

    results = []
    for case in cases:
        k = args.k or case["k"]
        print(f"\n▶ {case['id']}（{case['kind']}，k={k}）{case.get('title', '')}")
        results.append(await run_case(case, provider=provider, k=k, quiet=args.quiet))

    agg = _aggregate(results)
    agg["provider"] = provider_name
    agg["label"] = args.label

    md_path, json_path = _write_report(results, agg, args.label)
    print(f"\n{_LINE}\n pass@k = {agg['pass_at_k']}%｜pass^k = {agg['pass_pow_k']}%"
          f"｜unknown 均值 {agg['unknown_pct_avg']}%｜越界 {agg['overreach_total']}（门槛 0）\n{_LINE}")
    print(f"报告：{md_path.relative_to(ROOT)}\n数据：{json_path.relative_to(ROOT)}")

    if args.compare:
        print(f"\n【对比 · {args.compare.name}】\n{_compare(json_path, args.compare)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
