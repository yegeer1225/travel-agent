"""DeepSeek 接入探针。**M1 写图之前先跑这个；改了模型/思考开关后也跑。**

═══════════════════════════════════════════════════════════════
 为什么要单独一个探针脚本
═══════════════════════════════════════════════════════════════

本文件里的每一条结论都会决定架构怎么写。而"我以为"在这个环节**已经错过三次**
（400 的触发条件、structured output 可用性、思考字段在哪一层可见）。
所以把它固化成可重复跑的脚本，而不是让结论停在对话里。

═══════════════════════════════════════════════════════════════
 2026-09-15 首轮实测结论（跑这个脚本可复现）
═══════════════════════════════════════════════════════════════

**① 思考模式真实生效，但 `langchain-openai` 看不到 `reasoning_content`**

原始 HTTP 响应里 `message` 有 `reasoning_content`（实测 94~252 字），
`usage.completion_tokens_details.reasoning_tokens` 也有值。
但走 `langchain-openai` 时 `msg.additional_kwargs` 里**拿不到它** —— 被剥掉了。
→ 想用思考内容，必须自己发原始请求或自己扩展消息类。

**② `with_structured_output()` 默认那条路走不通**

```
400 invalid_request_error: This response_format type is unavailable now
```
`langchain-openai` 默认用 `response_format={"type":"json_schema"}`，**DeepSeek 不支持**。
逐项实测：

| `response_format` | 结果 |
|---|---|
| `json_schema`（带 `additionalProperties:false`） | ❌ 400 |
| `json_schema`（去掉 `additionalProperties`） | ❌ 400（**和字段无关，是整个类型不支持**） |
| **`json_object`** | ✅ 可用 |
| 不带 `response_format`（纯 prompt 要求） | ✅ 可用（也稳） |

→ `generate_plan` 走 **`json_object` + prompt 写清 schema + Pydantic 校验**，
不要用 `with_structured_output()` 的默认实现。
（`method="json_mode"` 理论上对应 `json_object`，但没必要绕一层抽象 ——
字段正确性最终还是靠 Pydantic 校验兜底，`json_object` 只保证"是合法 JSON"。）

**③ 🔴 「带 tools + 历史缺 `reasoning_content` → 400」未能复现**

这是对既有结论的**修正**，别把它当小事：

| 组合（flash / v4-pro 各测） | 结果 |
|---|---|
| 带 tools + 思考 + **原样回传** reasoning_content | ✅ 通过 |
| 带 tools + 思考 + **剥掉** reasoning_content | ✅ 通过 |
| 带 tools + **全程关思考** | ✅ 通过 |
| 不带 tools + 思考 + 剥掉 reasoning | ✅ 通过 |
| 连续 4 轮，每轮都剥掉 reasoning 再回传 | ✅ 通过 |

**注意：这不推翻"工具循环关思考"这个决定，只是把理由换掉。**
真正的理由是（这两条依然成立，而且更硬）：

1. **成本**：`reasoning_tokens` 计入 `completion_tokens`。实测一个最简请求
   = `prompt 42 + completion 163`，其中**推理占 160**。工具循环要跑 ≤8 轮，
   每轮都带推理 = 多烧数倍 token，而"搜武侯祠还是搜熊猫基地"根本不需要 CoT。
2. **确定性**：思考模式下 `temperature` 失效（官方原话 "will also have no effect"）。
   关掉之后 `temperature=0` 才真正生效 → **同样的输入得到同样的工具选择**，可复现。

**④ 复测时撞到的另一条 400（这条是真的，且会被踩到）**

```
An assistant message with 'tool_calls' must be followed by tool messages
responding to each 'tool_call_id'. (insufficient tool messages following tool_calls message)
```
模型**一次返回多个 `tool_calls`** 时，必须**逐个**用 `tool` 消息回应，漏一个就 400。
→ 手写 `tool_step` 时**不能只取 `tool_calls[0]`**。这是 `nodes.py` 里一个必须写对的地方。

用法：`../.venv/Scripts/python.exe scripts/probe_llm_structured.py`
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.llm import build_plan_llm, build_tool_llm  # noqa: E402
from app.providers.mock import MockAmapProvider  # noqa: E402
from app.tools import build_amap_tools, poi_pool_scope  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

URL = settings.llm_base_url.rstrip("/") + "/chat/completions"
HDR = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {settings.llm_api_key}",
}

PROBE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "search_poi",
            "description": "搜索地点",
            "parameters": {
                "type": "object",
                "properties": {"keyword": {"type": "string"}},
                "required": ["keyword"],
            },
        },
    }
]


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


def raw(body: dict) -> dict:
    """直接打原始接口。**故意不走 SDK** —— 探针要看到"服务端真实返回了什么"，
    多一层封装就多看一层假设。"""
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=HDR)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("error", {}).get("message", "")
        except Exception:
            detail = str(exc)
        return {"__http": exc.code, "__msg": detail[:300]}
    except Exception as exc:  # noqa: BLE001
        return {"__err": f"{type(exc).__name__}: {exc}"}


def failed(d: dict) -> bool:
    return "__http" in d or "__err" in d


def explain(d: dict) -> str:
    return f"HTTP {d['__http']}: {d['__msg']}" if "__http" in d else str(d.get("__err", d))[:200]


# ══════════════════════════════════════════════════════════════
#  [1] SDK 能不能通
# ══════════════════════════════════════════════════════════════


async def probe_sdk() -> None:
    print("\n[1] SDK 最小调用（tool llm，不带 tools）")
    llm = build_tool_llm()
    t0 = time.time()
    try:
        msg = await llm.ainvoke("用一句话回答：成都在哪个省？")
        text = (msg.content or "").strip()
        record("SDK 调用", bool(text), f"{time.time() - t0:.1f}s | {text[:50]}…")
    except Exception as exc:  # noqa: BLE001
        record("SDK 调用", False, f"{type(exc).__name__}: {str(exc)[:200]}")


# ══════════════════════════════════════════════════════════════
#  [2] agent_step 的同款配置：关思考 + tools
# ══════════════════════════════════════════════════════════════


async def probe_agent_step_config() -> None:
    print("\n[2] 关思考 + 带 tools（agent_step 同款）")
    provider = MockAmapProvider()
    tools = build_amap_tools(provider, default_city="成都")
    llm = build_tool_llm().bind_tools(tools)

    t0 = time.time()
    try:
        with poi_pool_scope() as pool:
            msg = await llm.ainvoke("帮我搜一下成都的武侯祠，我需要它的 id。")
            dt = time.time() - t0
            calls = list(getattr(msg, "tool_calls", None) or [])
            if calls:
                record(
                    "agent_step 配置可用",
                    True,
                    f"{dt:.1f}s | 模型自选工具 {[c['name'] for c in calls]} | args={calls[0]['args']}",
                )
                print(
                    "      ↑ 注意：这一步**只让模型决定**，并没有执行工具 —— "
                    "所以池子必然为空。\n"
                    "        执行是 `tool_step` 的事。这两步分开正是 "
                    "`agent_step ⇄ tool_step` 存在的原因。"
                )
            else:
                record("agent_step 配置可用", False, f"{dt:.1f}s | 未发起工具调用")
    except Exception as exc:  # noqa: BLE001
        record("agent_step 配置可用", False, f"{type(exc).__name__}: {str(exc)[:250]}")


# ══════════════════════════════════════════════════════════════
#  [3] reasoning_content 到底在哪一层可见
# ══════════════════════════════════════════════════════════════


async def probe_reasoning_visibility() -> None:
    print("\n[3] reasoning_content 在哪一层可见")

    d = raw(
        {
            "model": settings.llm_model_plan,
            "messages": [{"role": "user", "content": "成都3天带爸妈。只回答两个字：慢节奏"}],
            "thinking": {"type": "enabled"},
        }
    )
    if failed(d):
        record("原始 HTTP 拿到 reasoning", False, explain(d))
    else:
        msg = d["choices"][0]["message"]
        rc = msg.get("reasoning_content")
        tok = (d.get("usage") or {}).get("completion_tokens_details", {}).get("reasoning_tokens")
        record(
            "原始 HTTP 拿到 reasoning",
            bool(rc),
            f"{len(rc)} 字 | reasoning_tokens={tok} | keys={sorted(msg.keys())}",
        )

    try:
        msg2 = await build_plan_llm().ainvoke("只回答两个字：慢节奏")
        rc2 = (msg2.additional_kwargs or {}).get("reasoning_content")
        record(
            "SDK 层能拿到 reasoning",
            bool(rc2),
            "拿到了" if rc2 else "❌ 拿不到 —— langchain-openai 剥掉了（已知，不影响可用性）",
        )
    except Exception as exc:  # noqa: BLE001
        record("SDK 层调用", False, f"{type(exc).__name__}: {str(exc)[:150]}")


# ══════════════════════════════════════════════════════════════
#  [4] response_format 支持情况（决定 generate_plan 怎么写）
# ══════════════════════════════════════════════════════════════


def probe_response_format() -> None:
    print("\n[4] response_format 支持情况（决定 generate_plan 的写法）")
    base_msgs = [
        {"role": "system", "content": "只输出 JSON。输出含 city 和 days 两个字段的 JSON。"},
        {"role": "user", "content": "成都玩3天"},
    ]
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
        "required": ["city", "days"],
    }
    cases = {
        "json_schema（含 additionalProperties）": {
            "type": "json_schema",
            "json_schema": {"name": "P", "schema": {**schema, "additionalProperties": False}},
        },
        "json_schema（不含 additionalProperties）": {
            "type": "json_schema",
            "json_schema": {"name": "P", "schema": schema},
        },
        "json_object ← generate_plan 用这个": {"type": "json_object"},
        "不带 response_format（纯 prompt）": None,
    }

    for name, rf in cases.items():
        body: dict = {"model": settings.llm_model_tool, "messages": base_msgs}
        if rf is not None:
            body["response_format"] = rf
        d = raw(body)
        if failed(d):
            record(name, False, explain(d)[:150])
        else:
            content = d["choices"][0]["message"].get("content")
            try:
                json.loads(content or "")
                parsed = "且是合法 JSON"
            except Exception:  # noqa: BLE001
                parsed = "（非合法 JSON）"
            record(name, True, f"{str(content)[:50]}… {parsed}")

    # json_object + 开思考（generate_plan 的真实配置）
    d = raw(
        {
            "model": settings.llm_model_plan,
            "messages": base_msgs,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "enabled"},
        }
    )
    record(
        "json_object + 开思考 ← generate_plan 同款",
        not failed(d),
        explain(d) if failed(d) else "可用",
    )


# ══════════════════════════════════════════════════════════════
#  [5] 400 边界：多轮 + 剥掉 reasoning（含"漏回应 tool_calls"）
# ══════════════════════════════════════════════════════════════


def probe_400_boundary() -> None:
    print("\n[5] 400 边界复现")
    user = {"role": "user", "content": "搜一下成都武侯祠"}
    model = settings.llm_model_tool

    r1 = raw({"model": model, "messages": [user], "tools": PROBE_TOOL,
              "thinking": {"type": "enabled"}})
    if failed(r1):
        record("链式复现·第1轮", False, explain(r1))
        return
    m1 = r1["choices"][0]["message"]
    calls = m1.get("tool_calls") or []
    record("链式复现·第1轮", bool(calls),
           f"reasoning={'有' if m1.get('reasoning_content') else '无'} | tools={[c['function']['name'] for c in calls]}")
    if not calls:
        return

    # 剥掉 reasoning_content 后回传 —— 这正是"原结论"说要 400 的场景
    stripped = {k: v for k, v in m1.items() if k != "reasoning_content"}
    # ★ 逐个回应全部 tool_calls（漏一个就 400，这是实测撞到的真坑）
    tool_msgs = [
        {"role": "tool", "tool_call_id": c["id"], "content": "1 个候选：B001C07VJ2 | 成都武侯祠博物馆"}
        for c in calls
    ]
    r2 = raw({"model": model, "messages": [user, stripped, *tool_msgs], "tools": PROBE_TOOL,
              "thinking": {"type": "enabled"}})
    record(
        "带 tools + 剥掉 reasoning 回传",
        not failed(r2),
        explain(r2) if failed(r2) else "通过 —— 原记录的 400 未能复现",
    )

    # 故意漏回应一个 tool_calls（验证那条 400 是真的）
    if len(calls) >= 1:
        r3 = raw({"model": model, "messages": [user, stripped], "tools": PROBE_TOOL,
                  "thinking": {"type": "enabled"}})
        is_insufficient = "__http" in r3 and "tool_call_id" in str(r3.get("__msg", ""))
        record(
            "漏回应 tool_calls 会 400（真坑）",
            is_insufficient,
            explain(r3)[:170] if is_insufficient else "没触发（模型这次可能只调了 1 个工具，属正常）",
        )


async def main() -> int:
    print("=" * 74)
    print("DeepSeek 接入探针")
    print("=" * 74)
    print(f"  key        : {settings.llm_api_key[:8]}…（{len(settings.llm_api_key)} 位）")
    print(f"  base_url   : {settings.llm_base_url}")
    print(f"  tool model : {settings.llm_model_tool}  thinking={settings.llm_thinking_tool}")
    print(f"  plan model : {settings.llm_model_plan}  thinking={settings.llm_thinking_plan}")

    await probe_sdk()
    await probe_agent_step_config()
    await probe_reasoning_visibility()
    probe_response_format()
    probe_400_boundary()

    print()
    print("=" * 74)
    ok_count = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"结果：{ok_count}/{len(RESULTS)} 通过")
    print("注：标 ❌ 的未必是『坏了』 —— 例如『SDK 拿不到 reasoning』是已知事实，")
    print("    列在报告里是为了让下一个人不用重新发现它。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
