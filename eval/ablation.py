"""消融对照（ablation）：摘掉一条硬判据，同批热启动用例的抓错率掉多少？

═══════════════════════════════════════════════════════════════
 这个脚本回答什么问题
═══════════════════════════════════════════════════════════════

`run_eval.py` 报的是「现在多少分」。**分数本身不证明判据有用** ——
13 条用例全绿（pass@k = 100%）在面试里是弱信号，因为那是自己出题自己考。

消融回答的是另一半：「**把这条判据拿掉，会漏掉哪一个错**」——
掉下来的那部分，才是这条判据存在的价值。有对照组的数字才叫判据。

为什么不用重跑：热启动（粘贴）路径里 `validate_trip` 一次调用把结果**写进**
`trip.checks` 之后就进 `soft_check`，**判据不回流改行程**（`paste.py`），
所以「引擎摘掉判据 X」的行为 == 「同一份产物里去掉 code == X 的标记后重判」。
→ 基线真跑一次（真 LLM），之后**每个变体都是零 token 的离线重判**。

⚠️ 冷启动（一句话生成）**不适用**这个等价：那里校验失败会打回重排，
摘掉判据会改变后续轨迹，必须真跑。本脚本只做热启动。

═══════════════════════════════════════════════════════════════
 怎么跑
═══════════════════════════════════════════════════════════════

```bash
# 1. 跑基线 + 录存产物（耗 token，一次就好；数据源 mock = 高德零出账）
../.venv/Scripts/python.exe eval/ablation.py --provider mock --label ablate

# 2. 离线重判（零 token，可反复跑、可加变体）
../.venv/Scripts/python.exe eval/ablation.py \
    --from eval/reports/<stamp>-ablate-trips.json
```

⚠️ 和 `run_eval.py` 一样：`--provider` 只管**数据源**，LLM 永远真调、永远走 `.env`
（当前档位如实写进报告，别用猜的）。

变体怎么读（报告表）：
- `抓错率` = `should_flag` 命中的条数 / 总条数 —— 摘掉判据后掉多少就是这个判据的守备范围
- `误报` = `should_not_flag` 被误伤条数 —— 摘判据只会减少标记，所以这项只会变好，
  出现变化说明该判据在本批用例里只有"施压"没有"守备"（也是有价值的发现）
- `未覆盖` = 摘掉后**没有任何用例结果变化**的判据 —— 这批用例的盲区，不是判据没用
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from run_eval import (  # noqa: E402
    REPORTS_DIR,
    _grade_run,
    _load_cases,
    _run_hotstart,
)

# 5 条硬判据的 code（`validate.py` 全部 _hard(...) 落点，硬5 的"5"就是它）
HARD_CODES = ["poi_exists", "open_today", "reachable", "weather_conflict", "walk_load"]


# ══════════════════════════════════════════════════════════════
#  变体构造：从产物里摘掉一条判据的全部标记
# ══════════════════════════════════════════════════════════════


def _iter_checks(trip: dict):
    for day in trip.get("days") or []:
        for c in day.get("checks") or []:
            yield c
        for stop in day.get("stops") or []:
            for c in stop.get("checks") or []:
                yield c
    for c in trip.get("checks") or []:
        yield c


def _strip(trip: dict, codes: set[str]) -> dict:
    """去掉这些 code 的标记，并**重算 hard_errors**。

    `hard_errors`（= FAILED 硬判据条数，`validate.py` 汇总处）必须跟着重算：
    只看 checks 不看 summary 的话，产物自相矛盾，再看它的人会被误导。
    """
    t = copy.deepcopy(trip)
    if not codes:
        return t
    for day in t.get("days") or []:
        day["checks"] = [c for c in day.get("checks") or [] if c.get("code") not in codes]
        for stop in day.get("stops") or []:
            stop["checks"] = [c for c in stop.get("checks") or [] if c.get("code") not in codes]
    t["checks"] = [c for c in t.get("checks") or [] if c.get("code") not in codes]
    fails = sum(
        1 for c in _iter_checks(t)
        if c.get("level") == "hard" and c.get("status") == "fail"
    )
    if isinstance(t.get("summary"), dict):
        t["summary"]["hard_errors"] = fails
    return t


# ══════════════════════════════════════════════════════════════
#  基线跑：真跑热启动 + 录存产物
# ══════════════════════════════════════════════════════════════


def _build_provider(name: str):
    if name == "mock":
        from app.providers.mock import MockAmapProvider

        return MockAmapProvider()
    from app.providers.amap import AmapHttpProvider

    return AmapHttpProvider(settings.amap_webservice_key)


async def run_baseline(cases: list[dict], *, provider_name: str, k: int) -> dict:
    provider = _build_provider(provider_name)
    runs: list[dict] = []
    for case in cases:
        for i in range(k):
            t0 = time.monotonic()
            trip, err = await _run_hotstart(case, provider)
            elapsed = round(time.monotonic() - t0, 1)
            mark = "❌" if err else "✅"
            print(f"  {mark} {case['id']} 第 {i + 1}/{k} 次（{elapsed}s）{err or ''}")
            runs.append(
                {
                    "id": case["id"],
                    "run": i + 1,
                    "elapsed_s": elapsed,
                    "err": err,
                    "case": case,
                    "trip": trip.model_dump(mode="json") if hasattr(trip, "model_dump") else trip,
                }
            )
    return {
        "meta": {
            "label": None,  # 由调用方填
            "provider": provider_name,
            "stamp": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "k": k,
            "cases": len(cases),
            "llm_model_tool": settings.llm_model_tool,
            "llm_model_plan": settings.llm_model_plan,
            "note": "热启动路径产物录存：判据不回流改行程，故可离线做摘除消融",
        },
        "runs": runs,
    }


# ══════════════════════════════════════════════════════════════
#  画像：一个变体下的成绩
# ══════════════════════════════════════════════════════════════


def _tally(bundle: dict, codes: set[str]) -> dict:
    """给定摘掉的 code 集合，对该批产物逐条重判，汇总成绩。"""
    ok_ids, bad_ids = [], []
    flag_hit = flag_total = 0
    false_pos = 0
    errors = 0
    for run in bundle["runs"]:
        case, trip = run["case"], run["trip"]
        if run.get("err") or trip is None:
            errors += 1
            bad_ids.append(run["id"])
            continue
        grade = _grade_run(_strip(trip, codes), None, case)
        (ok_ids if grade["ok"] else bad_ids).append(run["id"])

        expect = case.get("expect") or {}
        wants = expect.get("should_flag") or []
        flag_total += len(wants)
        if wants:
            # 逐条口径与 run_eval._expect_failures 一致：按 code + day 匹配 fail
            where = [
                (f"d{d['day']}", c)
                for d in (trip.get("days") or [])
                for c in (d.get("checks") or [])
            ] + [
                (f"d{d['day']}s{s['seq']}", c)
                for d in (trip.get("days") or [])
                for s in (d.get("stops") or [])
                for c in (s.get("checks") or [])
            ]
            for w in wants:
                code, day = w.get("code"), w.get("day")
                hit = any(
                    c.get("code") == code and c.get("status") == "fail"
                    and (day is None or ww == f"d{day}" or ww.startswith(f"d{day}s"))
                    for ww, c in where
                    if c.get("code") not in codes  # ← 被摘掉的判据不再算命中
                )
                flag_hit += 1 if hit else 0
        false_pos += sum(
            1 for r in grade["reasons"] if r.startswith("should_not_flag 误报")
        )
    n = len(ok_ids) + len(bad_ids) or 1
    return {
        "passed": len(ok_ids),
        "total": n,
        "pass_pct": round(len(ok_ids) / n * 100, 1),
        "flag_hit": flag_hit,
        "flag_total": flag_total,
        "flag_pct": round(flag_hit / flag_total * 100, 1) if flag_total else None,
        "false_pos": false_pos,
        "failed_ids": bad_ids,
        "errors": errors,
    }


def analyze(bundle: dict) -> dict:
    variants = [("（基线）全部判据在位", set())]
    variants += [(f"摘除 {code}", {code}) for code in HARD_CODES]
    variants += [("摘除全部 5 条", set(HARD_CODES))]

    rows = []
    for title, codes in variants:
        v = _tally(bundle, codes)
        v["variant"] = title
        v["codes"] = sorted(codes)
        rows.append(v)

    base = rows[0]
    for r in rows[1:]:
        r["pass_delta"] = round(r["pass_pct"] - base["pass_pct"], 1)
        r["flag_delta"] = (
            round((r["flag_pct"] or 0) - (base["flag_pct"] or 0), 1)
            if base["flag_pct"] is not None else None
        )
        # 盲区判据：摘掉它，一条结果都不变 —— 该判据在**本批用例**里没人考它
        r["no_effect"] = r["pass_pct"] == base["pass_pct"] and r["flag_pct"] == base["flag_pct"]
    return {"baseline": base, "rows": rows, "meta": bundle["meta"]}


# ══════════════════════════════════════════════════════════════
#  报告
# ══════════════════════════════════════════════════════════════


def _write(analysis: dict, label: str) -> tuple[Path, Path]:
    meta, rows = analysis["meta"], analysis["rows"]
    base = analysis["baseline"]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS_DIR / f"{stamp}-{label}.json"
    md_path = REPORTS_DIR / f"{stamp}-{label}.md"

    json_path.write_text(
        json.dumps({"analysis": analysis}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    uncovered = [r["variant"] for r in rows[1:-1] if r.get("no_effect")]
    lines = [
        f"# 消融对照报告：{label}", "",
        f"- 时间：{stamp}｜数据源 provider：`{meta['provider']}`｜LLM：`{meta['llm_model_tool']}`",
        f"- 路径：热启动（粘贴校验）｜用例 {meta['cases']} 条 × k={meta['k']}",
        f"- 基线：pass {base['passed']}/{base['total']}（{base['pass_pct']}%）"
        f"｜抓错率 {base['flag_hit']}/{base['flag_total']}"
        f"（{base['flag_pct']}%）｜误报 {base['false_pos']}",
        "",
        "> 口径：摘除 = 该 code 的标记全部从产物中移除后重判。热启动路径判据不回流改行程，",
        "> 故等价于「引擎不跑这条判据」；冷启动（打回重排）不适用，未纳入。",
        "",
        "| 变体 | 通过 | 通过率 | Δ通过率 | 抓错率 | Δ抓错率 | 误报 | 掉落的用例 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        delta = "—" if "pass_delta" not in r else f"{r['pass_delta']:+.1f}%"
        fdelta = "—" if r.get("flag_delta") is None else f"{r['flag_delta']:+.1f}%"
        flag = "—" if r["flag_pct"] is None else f"{r['flag_hit']}/{r['flag_total']}（{r['flag_pct']}%）"
        failed = "、".join(r["failed_ids"]) or "—"
        lines.append(
            f"| {r['variant']} | {r['passed']}/{r['total']} | {r['pass_pct']}% | {delta} "
            f"| {flag} | {fdelta} | {r['false_pos']} | {failed} |"
        )
    lines += ["", "## 结论", ""]
    if uncovered:
        lines.append(
            "- **本批用例的盲区**："
            + "、".join(f"`{u}`" for u in uncovered)
            + f" 摘掉后无任何结果变化 —— 不是这些判据没用，"
            + f"而是本次这批 {meta['cases']} 条用例里没有考点压它们。"
        )
    dropped = [r for r in rows[1:-1] if not r.get("no_effect")]
    if dropped:
        for r in dropped:
            lines.append(
                f"- `{r['variant']}`：抓错率 {base['flag_pct']}% → {r['flag_pct']}%，"
                f"掉 {len(r['failed_ids'])} 条（{'、'.join(r['failed_ids'])}）"
            )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, json_path


# ══════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════


async def main() -> int:
    ap = argparse.ArgumentParser(description="硬判据摘除消融（热启动路径，可离线重判）")
    ap.add_argument("--cases", default="*.json", help="case 文件 glob（默认全部）")
    ap.add_argument("--k", type=int, default=1, help="热启动每条重复次数（默认 1）")
    ap.add_argument("--provider", choices=["mock", "real"], default="mock",
                    help="数据源（默认 mock：高德零出账；LLM 两种模式都真调）")
    ap.add_argument("--label", default="ablate", help="报告标签")
    ap.add_argument("--from", dest="from_trips", type=Path,
                    help="跳过真跑，直接读录好的产物做离线消融（零 token）")
    args = ap.parse_args()

    if args.from_trips:
        bundle = json.loads(args.from_trips.read_text(encoding="utf-8"))
        print(f"离线重判：{args.from_trips.name}"
              f"（{len(bundle['runs'])} 次运行，零 LLM 调用）")
    else:
        cases = [c for c in _load_cases(args.cases) if c["kind"] == "hotstart"]
        if not cases:
            print("没有热启动用例可跑")
            return 1
        skipped = [c["id"] for c in cases if c.get("requires") and c["requires"] != args.provider]
        cases = [c for c in cases if c["id"] not in skipped]
        print(f"消融基线跑｜provider={args.provider}｜LLM={settings.llm_model_tool}"
              f"｜热启动 {len(cases)} 条 × k={args.k}"
              + (f"｜跳过（requires 不匹配）：{'、'.join(skipped)}" if skipped else ""))
        bundle = await run_baseline(cases, provider_name=args.provider, k=args.k)
        bundle["meta"]["label"] = args.label
        if skipped:
            bundle["meta"]["skipped"] = skipped
        stamp = bundle["meta"]["stamp"]
        trips_path = REPORTS_DIR / f"{stamp}-{args.label}-trips.json"
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        trips_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"产物已录存：{trips_path.relative_to(ROOT)}（下次用 --from 免跑）")

    analysis = analyze(bundle)
    md_path, json_path = _write(analysis, args.label)
    base = analysis["baseline"]
    print(f"\n基线 pass {base['passed']}/{base['total']}｜抓错率 {base['flag_pct']}%")
    for r in analysis["rows"][1:]:
        print(f"  {r['variant']}：通过率 {r['pass_pct']}%（{r['pass_delta']:+.1f}%）"
              f"｜抓错率 {r['flag_pct']}%"
              + ("  ← 本批用例未覆盖" if r.get("no_effect") else ""))
    print(f"\n报告：{md_path.relative_to(ROOT)}\n数据：{json_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
