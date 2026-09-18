"""端到端计时探针：按节点打印 start/end 与耗时，量化并行收益。

用法：python scripts/measure_timing.py "用户需求"
只读观测，不改任何 state；跑完删除（Cleanup 铁律）。
"""

import asyncio
import sys
import time

sys.path.insert(0, ".")

from dotenv import load_dotenv

load_dotenv("../.env")

from langchain_core.messages import HumanMessage  # noqa: E402

from app.graph.graph import RECURSION_LIMIT, build_graph, build_runtime, initial_state  # noqa: E402


async def main() -> None:
    message = sys.argv[1] if len(sys.argv) > 1 else "10月2日出发，去北京玩5天，一个人，必须包含故宫和长城"
    runtime = build_runtime()
    graph = build_graph(runtime)
    config = {"recursion_limit": RECURSION_LIMIT}

    t0 = time.monotonic()
    node_t: dict[str, float] = {}
    first_tool_call_ts: float | None = None
    skeleton_ts: float | None = None

    async for ev in graph.astream_events(
        initial_state(message), config=config, version="v2"
    ):
        kind = ev.get("event")
        name = str(ev.get("name") or "")
        now = time.monotonic()

        if kind == "on_chain_start" and name in {
            "parse_intent", "ask_more", "make_skeleton", "agent_step",
            "tool_step", "generate_plan", "check_plan", "soft_check", "repair", "render",
        }:
            node_t[name] = now
            print(f"[{now - t0:7.1f}s] ▶ {name}", flush=True)
        elif kind == "on_chain_end" and name in node_t:
            started = node_t.pop(name)
            print(f"[{now - t0:7.1f}s] ■ {name}  耗时 {now - started:.1f}s", flush=True)
        elif kind == "on_chat_model_end" and name not in ("", "None"):
            pass  # 太密，不打印
        elif kind == "on_chain_end" and not ev.get("parent_ids"):
            print(f"[{now - t0:7.1f}s] ✅ 图跑完", flush=True)

    print(f"[{time.monotonic() - t0:7.1f}s] 总计", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
