"""命令行跑一次完整流程（M1 的验收入口）。

```bash
# 一句话需求 → 出行程
../.venv/Scripts/python.exe scripts/run_cli.py "下周三去成都玩3天，带爸妈，想吃点好的"

# 手动塞一条插话，验证 steering 在下一圈被吃进去
../.venv/Scripts/python.exe scripts/run_cli.py "成都玩3天" --pending "预算别超过人均800"

# 固定"今天"，让相对日期换算可复现（排查抽取问题时用）
../.venv/Scripts/python.exe scripts/run_cli.py "下周三去成都" --today 2026-09-16
```

═══════════════════════════════════════════════════════════════
 为什么要有这个脚本（而不是直接起 FastAPI 打接口）
═══════════════════════════════════════════════════════════════

M1 验收的是**图本身**：三层循环跑不跑得通、封闭世界拦不拦得住编造、
steering 会不会被吃进去。这些都跟 HTTP / SSE / 鉴权无关。

先起服务再测，会把"图的问题"和"接口的问题"混在一起 ——
而它们排查成本差一个量级（一个看 traceback，一个要看前端控制台）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date as Date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.graph.graph import build_runtime, initial_state, build_graph, RECURSION_LIMIT  # noqa: E402
from app.schemas import WeatherStatus  # noqa: E402

_LINE = "─" * 68


def _fmt_stop(stop: dict) -> str:
    """打印一站。**

    ⚠️ `cost_per_person` 显示成「人均」而不是「门票」 ——
    高德那个字段本来就是人均消费，门票数据大面积缺失（`schemas.py` 有实测记录）。
    文案错一个字，整个数据的可信度就没了：用户会以为"门票 73 元"，
    而它其实是"人均消费 73 元"，完全不同的两件事。
    """
    head = f"  {stop['seq']}. {stop['name']}  [{stop['poi_id']}]"
    time_part = ""
    if stop.get("arrive"):
        time_part = f"{stop['arrive']}"
        if stop.get("leave"):
            time_part += f" → {stop['leave']}"
        time_part += f"（{stop['stay_min']} 分钟）"
    else:
        time_part = f"（未定时刻，{stop['stay_min']} 分钟）"

    move = ""
    if stop.get("from_prev_km"):
        move = f"｜距上一站 {stop['from_prev_km']} km / {stop['from_prev_drive_min']} 分钟"

    extras: list[str] = []
    if stop.get("rating"):
        extras.append(f"评分{stop['rating']}")
    if stop.get("cost_per_person") is not None:
        extras.append(f"人均{stop['cost_per_person']}")
    if stop.get("open_time"):
        extras.append(f"开放{stop['open_time']}")

    lines = [head, f"     {time_part}{move}"]
    if extras:
        lines.append("     " + "｜".join(extras))
    if stop.get("match_reason"):
        lines.append(f"     为什么：{stop['match_reason']}")

    marks = []
    for chk in stop.get("checks") or []:
        icon = {"pass": "✅", "fail": "🔴", "unknown": "⚪"}.get(chk.get("status"), "?")
        marks.append(f"{chk.get('code')}{icon}")
    if marks:
        lines.append("     判据：" + "  ".join(marks))
        for chk in stop.get("checks") or []:
            if chk.get("status") != "pass" and chk.get("msg"):
                lines.append(f"       ↳ {chk['msg']}")
    return "\n".join(lines)


def print_result(state: dict) -> None:
    print(f"\n{_LINE}\n 结果\n{_LINE}")

    req = state.get("requirements") or {}
    if req:
        print("\n【需求】")
        print(json.dumps(req, ensure_ascii=False, indent=2))

    ask = state.get("ask")
    if ask:
        print("\n【追问（流程停在这里，等用户回答）】")
        print(ask)

    budget = (
        f"工具调用 {state.get('tool_call_count') or 0} 次"
        f"｜L2 轮数 {state.get('agent_rounds') or 0}"
        f"｜L3 打回 {state.get('check_rounds') or 0} 轮"
    )
    print(f"\n【预算消耗】{budget}")

    pool = state.get("collected_pois") or {}
    print(f"【候选池】{len(pool)} 个 POI 是工具真实返回过的")
    if pool:
        for poi in pool.values():
            print(f"    · {poi['name']}  [{poi['poi_id']}]")

    trip = state.get("trip")
    if trip:
        print(f"\n【行程】{trip['title']}（{trip['destination']}）")
        summary = trip["summary"]
        print(
            f"    总里程 {summary['total_distance_km']} km"
            f"｜站点 {summary['stop_count']} 个"
            f"｜人均参考 {summary['total_cost_per_person']}"
            f"｜硬错 {summary['hard_errors']}"
            f"｜软提示 {summary['soft_warnings']}"
        )
        for day in trip["days"]:
            weather = day.get("weather") or {}
            w = ""
            # ⚠️ 状态字面量是 `"ok"` 不是 `"available"`（`WeatherStatus.OK = "ok"`）。
            #    这里写错过一次，后果是**永远显示"天气无法判定"** —— 一个不报错的静默错误。
            #    所以断言取值一律从枚举来，不要手抄字面量。
            if weather.get("status") == WeatherStatus.OK:
                w = f"｜{weather.get('day_weather')} {weather.get('day_temp')}~{weather.get('night_temp')}℃"
            elif weather.get("status") == WeatherStatus.UNAVAILABLE:
                w = f"｜天气无法判定（{weather.get('note')}）"
            stats = day.get("day_stats") or {}
            print(
                f"\n  ── 第 {day['day']} 天 {day.get('date')}：{day.get('theme')}{w}"
                f"\n     当日 {stats.get('distance_km')} km / 车程 {stats.get('drive_min')} 分钟"
                f" / 步行约 {stats.get('walk_km')} km（估算）"
            )
            for stop in day.get("stops") or []:
                print(_fmt_stop(stop))

        validation = trip.get("validation") or {}
        if validation.get("remaining"):
            print("\n【剩余风险（已达打回上限，降级输出）】")
            for issue in validation["remaining"]:
                print(f"    ⚠️ [{issue['level']}] {issue['msg']}")

    err = state.get("error")
    if err:
        print(f"\n🔴 【错误】{err}")

    if not ask and not trip and not err:
        print("\n⚠️ 既没有追问、也没有行程、也没有错误 —— 图可能没跑到终点，检查条件边。")


async def main() -> int:
    parser = argparse.ArgumentParser(description="跑一次智能行程规划（命令行）")
    parser.add_argument("message", help="用户这一轮说的话")
    parser.add_argument(
        "--pending",
        action="append",
        default=[],
        help="模拟用户插话（steering），可重复传多次",
    )
    parser.add_argument("--today", help="固定'今天'为某个日期（YYYY-MM-DD），便于复现")
    parser.add_argument("--json", action="store_true", help="直接打印终态 JSON（调试用）")
    args = parser.parse_args()

    today = Date.fromisoformat(args.today) if args.today else None

    runtime = build_runtime(today=today)
    graph = build_graph(runtime)
    state = await graph.ainvoke(
        initial_state(args.message, pending_messages=args.pending),
        {"recursion_limit": RECURSION_LIMIT},
    )

    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"\n供应商：{runtime.provider.name}｜工具：{[t.name for t in runtime.tools]}")
        if args.pending:
            print(f"注入插话：{args.pending}")
        print_result(state)

    return 1 if state.get("error") and not state.get("trip") else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
