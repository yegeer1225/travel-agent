"""路由层的公共依赖：当前用户、repo 注入。

`get_current_user_id` 的历史是本项目**刻意规划的技术债**（api.md 1.1 / 方案.md
8.2 原话）：M5~M8 返回常量 1，所有路由从第一天起就从"当前用户"取身份写
WHERE；M9（现在）换成真 JWT 校验，**所有路由一行不用动** —— 而如果当初路由里
图省事写死 `user_id=1`，现在就得全文搜索替换，漏改的那处不会报错，只会静默泄露。

🔴 M9 起：token 缺失 / 签名错 / 过期 → **统一 401 `unauthorized`**，不区分原因
（区分 = 告诉攻击者"你的 token 只是旧了"）。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from app.api.errors import AppError
from app.api.security import parse_bearer, decode_token
from app.store.memory import InMemorySessionStore, InMemoryTripStore
from app.store.repo import SessionRepo, TripRepo, UserRepo


def get_current_user_id(request: Request) -> int:
    """从 `Authorization: Bearer <token>` 解析当前用户（M9）。

    校验只做两件事：**签名对不对、过没过期**（HS256 + exp）。
    不查库 —— session/trip 路由的隔离靠 `WHERE user_id`，uid 不存在时
    查不到东西自然 404；真正需要"用户确实存在"的只有 `/auth/me`，
    它自己会查 user_repo。
    """
    header = request.headers.get("authorization")
    try:
        token = parse_bearer(header)
        return decode_token(token)
    except Exception as exc:  # TokenError —— 统一 401，不区分缺失/过期/签名错
        raise AppError("unauthorized", "登录已失效，请重新登录", 401) from exc


def get_session_repo(request: Request) -> SessionRepo | InMemorySessionStore:
    return request.app.state.session_repo


def get_trip_repo(request: Request) -> TripRepo | InMemoryTripStore:
    return request.app.state.trip_repo


def get_user_repo(request: Request) -> UserRepo:
    return request.app.state.user_repo


def get_limiter(request: Request) -> Any:
    return request.app.state.limiter


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


def get_guide_repo(request: Request):
    return request.app.state.guide_repo


def get_comment_repo(request: Request):
    return request.app.state.comment_repo


def get_like_repo(request: Request):
    return request.app.state.like_repo


def get_favorite_repo(request: Request):
    return request.app.state.favorite_repo


__all__ = [
    "get_current_user_id",
    "get_nodes",
    "get_session_repo",
    "get_trip_repo",
    "get_user_repo",
    "get_guide_repo",
    "get_comment_repo",
    "get_like_repo",
    "get_favorite_repo",
    "get_limiter",
]
