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

from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.api.errors import register_handlers
from app.api.ratelimit import GLOBAL_IP, SlidingWindowLimiter, make_dependency
from app.api.registry import ActiveChatRegistry
from app.api.routes import auth, chat, health, sessions, trips
from app.config import settings
from app.store.db import connect
from app.store.memory import InMemorySessionStore, InMemoryTripStore, InMemoryUserStore
from app.store.repo import SessionRepo, TripRepo, UserRepo

# ── CORS（联调用，非契约）─────────────────────────────────────
# 豆包的前端跑 Vite dev server（localhost:5173），浏览器从那里 fetch /api
# 会先发预检。允许列表只放本地开发源 —— 不放 `*`，因为以后带 JWT 的
# 请求如果配 credentials，`*` 会直接被浏览器拒。
DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def create_app(
    *,
    session_repo: SessionRepo | InMemorySessionStore | None = None,
    trip_repo: TripRepo | InMemoryTripStore | None = None,
    user_repo: UserRepo | InMemoryUserStore | None = None,
    limiter: SlidingWindowLimiter | None = None,
    chat_factory: Any | None = None,
) -> FastAPI:
    """造一个应用实例。依赖都能注入 —— 测试换内存替身与 fake，生产不传（真库）。"""
    app = FastAPI(title="智能旅游规划系统", version="0.1.0")

    # ── 依赖落位（路由经 request.app.state 取用，见 deps.py）──
    if session_repo is None:
        session_repo = SessionRepo(conn_factory=lambda: connect(settings))
    if trip_repo is None:
        trip_repo = TripRepo(conn_factory=lambda: connect(settings))
    if user_repo is None:
        user_repo = UserRepo(conn_factory=lambda: connect(settings))
    app.state.session_repo = session_repo
    app.state.trip_repo = trip_repo
    app.state.user_repo = user_repo
    app.state.limiter = limiter or SlidingWindowLimiter()

    # ── 全局兜底限流（D28：300 / 分钟 · IP）──
    # 挂在 include_router 级 = 这个前缀下所有路由（含将来的 chat/paste）都被兜住；
    # 各端点自己的分层规则在 M6/M7 端点落地时加到各自路由上。
    global_limit = Depends(make_dependency(app.state.limiter, GLOBAL_IP))

    app.include_router(health.router, prefix="/api", dependencies=[global_limit])
    app.include_router(auth.router, prefix="/api", dependencies=[global_limit])
    app.include_router(sessions.router, prefix="/api", dependencies=[global_limit])
    app.include_router(trips.router, prefix="/api", dependencies=[global_limit])

    # ── M6：chat SSE ──
    # chat_factory 默认 = 真 LangGraph + AIOMySQLSaver（懒建，见 chat_stream.py）；
    # 测试注入 fake（只实现三个方法），全链路不连库不连 LLM。
    if chat_factory is None:
        from app.api.chat_stream import make_default_chat_factory

        chat_factory = make_default_chat_factory()
    app.state.chat_factory = chat_factory
    app.state.active_chats = ActiveChatRegistry()

    # chat 自身的限流（10/h·30/天）在路由体内手动查 —— 它要先读 body 判断
    # 是不是 steer（插队不占配额），不能放在"读 body 之前"的依赖里。
    app.include_router(chat.router, prefix="/api", dependencies=[global_limit])

    register_handlers(app)

    # ── CORS：只对 /api 生效的判断交给浏览器侧（origin 不在名单就不给头）──
    # 这是联调配置不是接口契约 —— 生产部署若前后端同源，删掉这两行即可。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── 冒烟验证页（M5 联调自检，与产品前端无关）──
    # 单文件、零构建，由后端自己服务（同源，不受 CORS 影响）。
    # 用途：起服务后浏览器开 /verify，把 M5 读路径+错误形状全打一遍。
    static_dir = Path(__file__).resolve().parents[2] / "static"
    if static_dir.is_dir():
        app.mount("/verify", StaticFiles(directory=static_dir, html=True), name="verify")
    return app


app = create_app()
