"""路由层的公共依赖：当前用户、repo 注入。

`get_current_user_id` 现在返回常量 1 —— 这是**刻意的技术债**
（api.md 1.1 写明 M5~M8 都这样）。它的意义：所有路由从第一天起
就从"当前用户"取身份写 WHERE，M9 换成真 JWT 校验时**只改这一个函数**。
如果路由里图省事直接写死 `user_id=1`，M9 就得全文搜索替换 ——
而漏改的那一处不会报错，只会静默泄露（技术方案 622 行的原话）。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from app.store.memory import InMemorySessionStore, InMemoryTripStore
from app.store.repo import SessionRepo, TripRepo


def get_current_user_id() -> int:
    """当前用户。M9 前恒为 1（见模块 docstring）。"""
    return 1


def get_session_repo(request: Request) -> SessionRepo | InMemorySessionStore:
    return request.app.state.session_repo


def get_trip_repo(request: Request) -> TripRepo | InMemoryTripStore:
    return request.app.state.trip_repo


def get_nodes(request: Request) -> Any:
    """Agent 运行时（Nodes：provider + 各 LLM）。M7 paste/recheck 用。

    **懒建**：首次真正用到才组装（构造 ChatOpenAI 不联网，但会读配置 ——
    惰性让"没有 key 也能起服务跑读路径"保持成立，与 chat_factory 同理）。
    测试注入 fake（make_nodes）后永远走不到这里。
    """
    nodes = getattr(request.app.state, "nodes", None)
    if nodes is None:
        from app.graph.graph import build_runtime

        nodes = build_runtime()
        request.app.state.nodes = nodes
    return nodes


__all__ = ["get_current_user_id", "get_nodes", "get_session_repo", "get_trip_repo"]
