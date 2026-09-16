"""SSE 公共工具：帧格式 + 响应头 + 心跳包装（M7 从 chat 抽出，paste 复用）。

**帧格式照 `docs/api.md` 3.7 ①**，那是冻结契约 —— 这里是它唯一的实现点，
chat 与 paste 都从这里拿，改这里等于同时改两条流。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

# 心跳间隔（契约：15s）。取 14s 留出余量，防止代理在整 15s 处掐线。
HEARTBEAT_SECONDS = 14.0

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",  # nginx 不缓冲（本地开发用不到，部署时救命）
}


def frame(seq: int, evt: Any) -> str:
    """一帧 SSE：`id:` + `data:`（单行 JSON，ensure_ascii=False）+ 空行。

    ⚠️ JSON 里**绝不能有换行**（前端按行切）—— `json.dumps` 默认不会产生
    换行，前提是喂进来的是 Pydantic 模型 dump 而不是原始文本。
    """
    body = json.dumps(evt.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    return f"id: {seq}\ndata: {body}\n\n"


class _Done:
    """心跳包装的哨兵：producer 结束的信号（None 可能有歧义，用私有类）。"""

    def __repr__(self) -> str:  # pragma: no cover —— 调试辅助
        return "<SSE-DONE>"


_DONE = _Done()


async def heartbeat_stream(
    source: AsyncIterator[dict[str, Any]],
    *,
    timeout: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[dict[str, Any]]:
    """给事件流包一层心跳：底层卡住超过 `timeout` 就吐 `{"heartbeat": True}`。

    🔴 **producer 必须在独立 task 里跑**，不能对 `source.__anext__()` 直接
    `wait_for` —— 超时取消会**打断生成器本体**，它就再也续不上了。
    （chat_stream 的 queue 模式就是这个道理，paste 流走这里的等价实现。）
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def produce() -> None:
        try:
            async for item in source:
                queue.put_nowait(item)
            queue.put_nowait(_DONE)
        except asyncio.CancelledError:
            raise  # 客户端断开 → 正常退出
        except Exception as exc:  # noqa: BLE001 —— 流里出错也只能在流里报（HTTP 已 200）
            from app.schemas import ErrorEvent

            queue.put_nowait({"event": ErrorEvent(
                type="error", code="internal_error",
                msg=f"{type(exc).__name__}: {exc}"[:200],
            )})
            queue.put_nowait(_DONE)

    task = asyncio.create_task(produce())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                yield {"heartbeat": True}
                continue
            if isinstance(item, _Done):
                break
            yield item
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


__all__ = ["HEARTBEAT_SECONDS", "SSE_HEADERS", "frame", "heartbeat_stream"]
