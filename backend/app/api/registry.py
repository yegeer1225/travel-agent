"""同会话并发流守卫（D28："同一个 session_id 同时只允许一个在跑的流"）。

═══════════════════════════════════════════════════════════════
 为什么是进程内 dict 而不是数据库锁
═══════════════════════════════════════════════════════════════

这条守卫保的是"一个会话的 checkpoint 不被两路并发写"——
它只在**单进程内**有意义。多 worker 部署时各进程各管各的，
这条就漏了 —— 和 D28 限流同一条重审触发（多实例 → Redis）。
在单 worker 的部署形态下（当前唯一形态），它是精确的。

⚠️ 进程重启 = 全部清空：如果重启时恰好有流在跑，客户端会收到断流
（EOF 没 done），重连走 Last-Event-ID 恢复 —— 恢复路径不经过本注册表
（`resume` 请求同样 start/finish，保证恢复期间也不会有第二个流挤进来）。
"""

from __future__ import annotations

from typing import Any


class ActiveChatRegistry:
    """session_id → 正在跑的 ChatHandle。进出配对，重复 start 返回 False。"""

    def __init__(self) -> None:
        self._active: dict[str, Any] = {}

    def is_active(self, session_id: str) -> bool:
        return session_id in self._active

    def handle_of(self, session_id: str) -> Any | None:
        """正在跑的句柄（steer 用）；没在跑返回 None。"""
        return self._active.get(session_id)

    def start(self, session_id: str, handle: Any) -> bool:
        """占住会话。已被占（理论上传进来前查过 is_active，防御并发窗口）返回 False。"""
        if session_id in self._active:
            return False
        self._active[session_id] = handle
        return True

    def finish(self, session_id: str) -> None:
        """释放。幂等 —— `_sse_body` 的 finally 与路由的异常路径都可能调。"""
        self._active.pop(session_id, None)


__all__ = ["ActiveChatRegistry"]
