"""从 backend/app/schemas.py 生成前端 TypeScript 类型（docs/types.ts）。

用法（在 backend/ 目录下）：
    ../.venv/Scripts/python.exe scripts/gen_ts_types.py

────────────────────────────────────────────────────────────
为什么必须有这个脚本
────────────────────────────────────────────────────────────
契约的**唯一事实源**是 `schemas.py`。前端类型如果手工抄一遍：
  · 两天后必然不一致
  · 不一致 **不会报错** —— 只是某个字段运行时是 `undefined`，
    而前端把它当 `null` 处理（或不处理），页面局部错乱且极难定位
自动生成后：改契约 → 重跑这条命令 → **TS 编译期**把所有受影响的地方全指出来。

设计约定（生成结果里也写了）：
  · 字段名保持 **snake_case**，前端不做 camelCase 转换（省一层转换和一类 bug）
  · `null` 是**有意义的业务值**（"接口没给"），不要用 `?.` / `?? ''` 抹平它
  · **所有字段都是必填**（不带 `?`）—— 因为后端响应一律全字段 dump，
    不用 `exclude_unset` / `exclude_none`。这条不变量让 TS 类型和线上数据严格一致，
    代价是前端手写 mock 数据时必须补全字段（值得）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
PROJ = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app import schemas  # noqa: E402

from pydantic import BaseModel  # noqa: E402

OUT = PROJ / "docs" / "types.ts"

HEADER = """/**
 * ⚠️ 本文件**自动生成**，不要手改。
 *
 * 来源：backend/app/schemas.py（契约唯一事实源）
 * 生成：cd backend && ../.venv/Scripts/python.exe scripts/gen_ts_types.py
 *
 * 三条约定（照着写就不会错）：
 * 1. 字段名是 **snake_case**，后端不下发 camelCase，前端也不转
 * 2. `| null` 是**业务值**（"接口没给这个数据"），不是"还没加载"。
 *    ⚠️ 不要用 `?? ''` / `?? 0` 抹平它 —— 0 是"免费"、'' 是"空字符串"，
 *    跟 null 是两件事，抹平了就是把"没数据"伪装成"有数据"（见 docs/api.md 4.2）
 * 3. **字段一律必填**（没有 `?`）：后端响应是全字段 dump，字段一定在，值可能是 null。
 *    （在前端手写 mock 数据时需要补全所有字段）
 */
"""

# 泛型与联合类型无法从 JSON Schema 还原，单独手写（见 build() 末尾）
MANUAL = """
// ── 下面这段是脚本手写的，不是生成的 ─────────────────────────

/** 分页外壳。所有列表接口都是它，前端只写一次解包逻辑 */
export interface Page<T> {
  items: T[]
  /** 满足条件的**总数**，不是本页条数 */
  total: number
  limit: number
  offset: number
}

/** SSE 每条 `data:` 反序列化后的联合体。按 `type` 分发 */
export type SSEEvent =
  | SessionEvent
  | NodeEvent
  | ToolCallEvent
  | ToolResultEvent
  | TokenEvent
  | TripEvent
  | CheckEvent
  | DoneEvent
  | ErrorEvent

/** 前端必须**忽略未知 type**（方便后端加新事件而不破坏老前端） */
export function isKnownEvent(evt: { type: string }): evt is SSEEvent {
  return [
    'session',
    'node',
    'tool_call',
    'tool_result',
    'token',
    'trip',
    'check',
    'done',
    'error',
  ].includes(evt.type)
}
"""


def ts_type(schema: dict[str, Any]) -> str:
    """把一个 JSON Schema 节点翻译成 TS 类型。认不出来的**直接抛错**，不猜。"""
    if not schema:
        return "unknown"

    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]

    if "enum" in schema:
        return " | ".join(json.dumps(v, ensure_ascii=False) for v in schema["enum"])

    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)

    for key in ("anyOf", "oneOf"):
        if key in schema:
            parts: list[str] = []
            for sub in schema[key]:
                t = ts_type(sub)
                if t not in parts:
                    parts.append(t)
            return " | ".join(parts)

    if "allOf" in schema:
        parts = [ts_type(s) for s in schema["allOf"]]
        return " & ".join(dict.fromkeys(parts))

    t = schema.get("type")

    if isinstance(t, list):  # 如 ["string", "null"]
        parts = [ts_type({"type": x}) for x in t]
        return " | ".join(dict.fromkeys(parts))

    if t == "null":
        return "null"
    if t == "string":
        return "string"
    if t in ("integer", "number"):
        return "number"
    if t == "boolean":
        return "boolean"

    if t == "array":
        inner = ts_type(schema.get("items") or {})
        if "|" in inner:
            inner = f"({inner})"
        return f"{inner}[]"

    if t == "object":
        ap = schema.get("additionalProperties")
        if isinstance(ap, dict):
            return f"Record<string, {ts_type(ap)}>"
        if not schema.get("properties"):
            return "Record<string, unknown>"
        raise NotImplementedError(f"内联匿名对象未支持：{json.dumps(schema, ensure_ascii=False)[:200]}")

    raise NotImplementedError(f"无法翻译的 schema：{json.dumps(schema, ensure_ascii=False)[:200]}")


def collect_defs() -> dict[str, dict[str, Any]]:
    """把 schemas.py 里所有模型 / 枚举的 JSON Schema 收集到一张表（含嵌套 $defs）。"""
    defs: dict[str, dict[str, Any]] = {}
    roots: dict[str, dict[str, Any]] = {}

    for name in schemas.__all__:
        obj = getattr(schemas, name, None)
        if not (isinstance(obj, type) and issubclass(obj, BaseModel)) or name == "Page":
            continue
        try:
            schema = obj.model_json_schema(ref_template="#/$defs/{model}")
        except Exception as exc:  # 泛型 / 无 schema 的模型
            print(f"  ! 跳过 {name}: {type(exc).__name__}", file=sys.stderr)
            continue

        root = dict(schema)
        for sub_name, sub in (root.pop("$defs", None) or {}).items():
            defs.setdefault(sub_name, sub)
        root.pop("title", None)
        roots[name] = root

    return {**defs, **roots}


def render(name: str, schema: dict[str, Any]) -> str:
    """一个 def → 一段 TS。"""
    if "enum" in schema:
        return f"export type {name} =\n  | " + "\n  | ".join(
            json.dumps(v, ensure_ascii=False) for v in schema["enum"]
        )

    if schema.get("type") != "object":
        return f"export type {name} = {ts_type(schema)}"

    props: dict[str, Any] = schema.get("properties") or {}

    lines = [f"export interface {name} {{"]
    for field, sub in props.items():
        desc = sub.get("description")
        if desc:
            for para in str(desc).strip().splitlines():
                lines.append(f"  /** {para.strip()} */" if para.strip() else "  *")
        # ⚠️ 不用 pydantic 的 required —— 后端全字段 dump，所有字段都在
        lines.append(f"  {field}: {ts_type(sub)}")
    lines.append("}")
    return "\n".join(lines)


def main() -> int:
    defs = collect_defs()

    # 枚举先出（前端 import 起来更自然），模型按 collect 顺序
    enums = {k: v for k, v in defs.items() if "enum" in v and v.get("type") == "string"}
    models = {k: v for k, v in defs.items() if k not in enums}

    chunks: list[str] = [HEADER]
    chunks.append(f"// {'=' * 62}\n//  枚举（前端可当 union type 直接用）\n// {'=' * 62}\n")
    chunks += [render(k, v) + "\n" for k, v in enums.items()]
    chunks.append(f"\n// {'=' * 62}\n//  结构体\n// {'=' * 62}\n")
    chunks += [render(k, v) + "\n" for k, v in models.items()]
    chunks.append(MANUAL)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(chunks), encoding="utf-8")

    print(f"✅ 已生成 {OUT.relative_to(PROJ)}")
    print(f"   枚举 {len(enums)} 个 / 结构体 {len(models)} 个 / 共 {len(defs)} 个类型")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
