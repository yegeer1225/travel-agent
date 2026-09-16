"""FastAPI 应用组装（M5）。

═══════════════════════════════════════════════════════════════
 组装点只有这一个
═══════════════════════════════════════════════════════════════

"用 MySQL repo 还是内存 repo"在 `create_app` 的参数上决定 ——
路由代码对两者零感知（同签名，见 `store/memory.py` 的注释）。
测试传 InMemory；`scripts/verify_mysql_api.py` 不传（真库）。

启动（开发）：
    cd backend && ../.venv/Scripts/python.exe -m uvicorn app.api.main:app --port 8000
    # 前端 Vite 把 /api 代理到 127.0.0.1:8000（见 前端交接.md），所以路由带 /api 前缀
"""

from __future__ import annotations

from fastapi import Depends, FastAPI

from app.api.errors import register_handlers
from app.api.ratelimit import GLOBAL_IP, SlidingWindowLimiter, make_dependency
from app.api.routes import health, sessions, trips
from app.config import settings
from app.store.db import connect
from app.store.memory import InMemorySessionStore, InMemoryTripStore
from app.store.repo import SessionRepo, TripRepo


def create_app(
    *,
    session_repo: SessionRepo | InMemorySessionStore | None = None,
    trip_repo: TripRepo | InMemoryTripStore | None = None,
    limiter: SlidingWindowLimiter | None = None,
) -> FastAPI:
    """造一个应用实例。三个依赖都能注入 —— 测试换内存替身，生产不传（真库）。"""
    app = FastAPI(title="智能旅游规划系统", version="0.1.0")

    # ── 依赖落位（路由经 request.app.state 取用，见 deps.py）──
    if session_repo is None:
        session_repo = SessionRepo(conn_factory=lambda: connect(settings))
    if trip_repo is None:
        trip_repo = TripRepo(conn_factory=lambda: connect(settings))
    app.state.session_repo = session_repo
    app.state.trip_repo = trip_repo
    app.state.limiter = limiter or SlidingWindowLimiter()

    # ── 全局兜底限流（D28：300 / 分钟 · IP）──
    # 挂在 include_router 级 = 这个前缀下所有路由（含将来的 chat/paste）都被兜住；
    # 各端点自己的分层规则在 M6/M7 端点落地时加到各自路由上。
    global_limit = Depends(make_dependency(app.state.limiter, GLOBAL_IP))

    app.include_router(health.router, prefix="/api", dependencies=[global_limit])
    app.include_router(sessions.router, prefix="/api", dependencies=[global_limit])
    app.include_router(trips.router, prefix="/api", dependencies=[global_limit])

    register_handlers(app)
    return app


app = create_app()
