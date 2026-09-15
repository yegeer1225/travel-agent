"""风险 2 探针：langgraph-checkpoint-mysql（第三方包）能否与 LangGraph 1.2 配合。

背景
----
`技术方案.md` 风险 2 挂着的唯一不确定性：checkpoint 存 MySQL 用的是**第三方包**
（`langgraph-checkpoint-mysql`，非 LangGraph 官方），它声明 `langgraph-checkpoint>=2.1.2`
**没有上界**，而本地核心包已是 4.2.0。装得上不代表跑得通 —— 这个脚本只干一件事：
把"能不能跑通"从**推测**变成**实测结论**。

判定标准（三条全过才算过）
------------------------
1. `.setup()` 能在 MySQL 8 上把表建出来（22 个 migration 全过）
2. 同步 saver：写入 → 换一个**全新 saver 实例**仍能读出同一 thread 的状态
   （换实例是关键：内存里的东西换实例就没了，读得到才证明真落库）
3. 异步 saver（`AIOMySQLSaver`）：`astream` 能跑通，`aget_state` 能读出

用法
----
    cd backend
    ../.venv/Scripts/python.exe scripts/check_checkpoint_mysql.py

退出码 0 = 全过；非 0 = 有失败项（打印在哪一步挂的）。
"""

from __future__ import annotations

import asyncio
import operator
import sys
from pathlib import Path
from typing import Annotated, TypedDict

# ---- 读 .env：用 python-dotenv，和主应用（config.py）走同一条路径 ----
# 一开始这里是自己手写解析的，但 dotenv 本来就已装、且是应用真正会用的加载器 ——
# 探针跑的是"应用真实的配置路径"，比省一个已被装上的依赖更有价值。
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
ENV = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}
HOST = ENV.get("MYSQL_HOST", "127.0.0.1")
PORT = int(ENV.get("MYSQL_PORT", "3306"))
USER = ENV.get("MYSQL_USER", "travel")
PWD = ENV["MYSQL_PASSWORD"]
DB = ENV.get("MYSQL_DB", "travel_agent")

SYNC_DSN = f"mysql://{USER}:{PWD}@{HOST}:{PORT}/{DB}"
ASYNC_DSN = f"mysql+aiomysql://{USER}:{PWD}@{HOST}:{PORT}/{DB}"


# ---- 一个最小但真实的状态图：有 reducer、有跨步累加，能看出历史有没有留下 ----
class S(TypedDict):
    n: int
    log: Annotated[list[str], operator.add]


def step(state: S) -> S:
    n = state["n"] + 1
    return {"n": n, "log": [f"step{n}"]}


def build_graph(checkpointer):
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(S)
    g.add_node("step", step)
    g.add_edge(START, "step")
    g.add_edge("step", END)
    return g.compile(checkpointer=checkpointer)


RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


# ======================== 0. 预检：排序规则必须自洽 ========================
def preflight() -> bool:
    """挡住 1267 `Illegal mix of collations` —— 这个坑实测踩过一次，代价一个下午。

    机理：驱动（PyMySQL / aiomysql）的 charset='utf8mb4' 会发 `SET NAMES utf8mb4`，
    服务端随即把 `collation_connection` 设为【utf8mb4 这个字符集的默认排序规则】，
    **跟 `collation_server` 没关系**。若 compose 里把 collation_server 写成别的
    （比如 utf8mb4_unicode_ci），就会出现：

        表里的列（继承 collation_server） = utf8mb4_unicode_ci
        连接里的字面量（SET NAMES 决定） = utf8mb4_0900_ai_ci

    两者在同一个查询里相遇 -> 报错 1267。

    所以判据只有一条：**collation_server 必须等于 collation_connection**。
    """
    print("\n[0] 预检：排序规则自洽性")
    import pymysql

    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PWD,
                           database=DB, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT @@collation_server, @@character_set_server, "
                        "@@collation_database, @@collation_connection")
            cs, chs, cd, cc = cur.fetchone()
            print(f"     character_set_server = {chs}")
            print(f"     collation_server     = {cs}   ← docker-compose.yml 里设的")
            print(f"     collation_database   = {cd}")
            print(f"     collation_connection = {cc}   ← 驱动 SET NAMES 决定的")

            ok = cs == cc
            record(
                "collation_server == collation_connection",
                ok,
                "" if ok else (
                    f"不一致（{cs} vs {cc}）—— 这会导致 1267 Illegal mix of collations。"
                    f"改 docker-compose.yml 的 --collation-server 与 {cc} 对齐后"
                    f"执行 docker compose down -v && docker compose up -d"
                ),
            )
            if not ok:
                return False

            # 继承 collation_server 建的库，其默认值也得一致
            ok2 = cd == cc
            record("collation_database == collation_connection", ok2,
                   "" if ok2 else f"{cd} vs {cc} —— 需要 down -v 重建库")
            return ok and ok2
    finally:
        conn.close()


# ======================== 1. 同步 saver ========================
def test_sync() -> None:
    print("\n[1] 同步 PyMySQLSaver")
    from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver

    with PyMySQLSaver.from_conn_string(SYNC_DSN) as saver:
        saver.setup()
        record("setup() 建表", True)

        graph = build_graph(saver)
        cfg = {"configurable": {"thread_id": "spike-sync"}}
        out = graph.invoke({"n": 0, "log": []}, cfg)
        record("invoke() 写入", out["n"] == 1, f"n={out['n']} log={out['log']}")

        hist = list(graph.get_state_history(cfg))
        record("get_state_history() 有历史", len(hist) >= 2, f"{len(hist)} 个 checkpoint")

    # ← 上一个 saver 已经关闭。重新开一个全新实例读同一个 thread
    with PyMySQLSaver.from_conn_string(SYNC_DSN) as saver2:
        graph2 = build_graph(saver2)
        cfg = {"configurable": {"thread_id": "spike-sync"}}
        st = graph2.get_state(cfg)
        ok = bool(st.values) and st.values.get("n") == 1
        record(
            "换新实例仍能读出状态（= 真落库）",
            ok,
            f"values={st.values}",
        )

        # 第二次 invoke：reducer 必须接着累加，而不是从 0 重来
        out2 = graph2.invoke({"log": ["again"]}, cfg)
        record(
            "reducer 在恢复的状态上继续累加",
            out2["n"] == 2 and out2["log"] == ["step1", "again", "step2"],
            f"n={out2['n']} log={out2['log']}",
        )

        # 清理，别给库留垃圾
        saver2.delete_thread("spike-sync")
        record("delete_thread() 清理", True)


# ======================== 2. 异步 saver ========================
async def test_async() -> None:
    print("\n[2] 异步 AIOMySQLSaver（应用里真正会用到的那个）")
    from langgraph.checkpoint.mysql.aio import AIOMySQLSaver

    async with AIOMySQLSaver.from_conn_string(ASYNC_DSN) as saver:
        # ⚠️ 异步 saver 的建表方法不叫 asetup，就叫 setup —— 只不过它本身是协程。
        #    第一版脚本写成 `await saver.asetup()` 直接 AttributeError。判据：
        #    inspect.iscoroutinefunction(AIOMySQLSaver.setup) is True
        await saver.setup()
        record("await setup()（异步版同名方法）", True)

        graph = build_graph(saver)
        cfg = {"configurable": {"thread_id": "spike-async"}}

        # astream 是 FastAPI 那条链路（astream_events 的底座）最接近的模拟
        chunks = [c async for c in graph.astream({"n": 0, "log": []}, cfg)]
        record("astream() 流式跑通", len(chunks) >= 1, f"{len(chunks)} 个 chunk")

        st = await graph.aget_state(cfg)
        record("aget_state() 读出", st.values.get("n") == 1, f"values={st.values}")

        hist = [h async for h in graph.aget_state_history(cfg)]
        record("aget_state_history()", len(hist) >= 2, f"{len(hist)} 个 checkpoint")

    async with AIOMySQLSaver.from_conn_string(ASYNC_DSN) as saver2:
        graph2 = build_graph(saver2)
        cfg = {"configurable": {"thread_id": "spike-async"}}
        st = await graph2.aget_state(cfg)
        record("换新异步实例仍能读出", st.values.get("n") == 1, f"values={st.values}")
        await saver2.adelete_thread("spike-async")
        record("adelete_thread() 清理", True)


# ======================== 3. 表与行数 ========================
def test_tables() -> None:
    print("\n[3] 库里实际建出了什么表")
    import pymysql

    conn = pymysql.connect(host=HOST, port=PORT, user=USER, password=PWD,
                           database=DB, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = sorted(r[0] for r in cur.fetchall())
            print("     表:", tables)
            record("checkpoint 系列表已建出",
                   any(t.startswith("checkpoint") for t in tables),
                   f"{len(tables)} 张表")

            for t in tables:
                cur.execute(f"SELECT COUNT(*) FROM `{t}`")
                print(f"       {t:34} {cur.fetchone()[0]:>5} 行")

            # 清理后应该没有残留的 spike 数据
            cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id LIKE 'spike-%'")
            left = cur.fetchone()[0]
            record("探针数据已清理干净", left == 0, f"残留 {left} 行")

            cur.execute("SELECT VERSION()")
            print("     MySQL 版本:", cur.fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    print("=" * 72)
    print("checkpoint-mysql 兼容性探针")
    print(f"  DSN(sync)  = mysql://{USER}:***@{HOST}:{PORT}/{DB}")
    print(f"  DSN(async) = mysql+aiomysql://{USER}:***@{HOST}:{PORT}/{DB}")
    print("=" * 72)

    from importlib.metadata import version as pkgver

    # ⚠️ langgraph 没有 __version__ 属性（试过，AttributeError），只能用 importlib.metadata
    for pkg in ("langgraph", "langgraph-checkpoint", "langgraph-checkpoint-mysql",
                "PyMySQL", "aiomysql"):
        try:
            print(f"  {pkg:26} {pkgver(pkg)}")
        except Exception:
            print(f"  {pkg:26} (未安装)")

    try:
        healthy = preflight()
    except Exception as e:
        record("预检连接", False, f"{type(e).__name__}: {e}")
        healthy = False

    if healthy:
        try:
            test_sync()
        except Exception as e:
            import traceback
            traceback.print_exc()
            record("同步链路", False, f"{type(e).__name__}: {e}")

        try:
            asyncio.run(test_async())
        except Exception as e:
            import traceback
            traceback.print_exc()
            record("异步链路", False, f"{type(e).__name__}: {e}")
    else:
        print("\n⛔ 预检没过，后面的测试跳过（排序规则不一致时它们必然失败，跑也是白跑）")

    try:
        test_tables()
    except Exception as e:
        record("表检查", False, f"{type(e).__name__}: {e}")

    print("\n" + "=" * 72)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"结论：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        print("失败项：")
        for n in failed:
            print("  -", n)
        return 1
    print("✅ checkpoint 可以存 MySQL —— 风险 2 关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
