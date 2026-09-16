"""API 层测试的公共夹具：**InMemory store + TestClient，绝不连 MySQL**。

（tests 不连库是 `requirements.txt` 里写死的纪律；
真库验收在 `scripts/verify_mysql_api.py`，Docker 起来后跑。）
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.ratelimit import SlidingWindowLimiter
from app.api.security import create_token
from app.schemas import Trip, TripSummary
from app.store.memory import (
    InMemoryCommentStore,
    InMemoryFavoriteStore,
    InMemoryGuideStore,
    InMemoryLikeStore,
    InMemorySessionStore,
    InMemoryTripStore,
    InMemoryUserStore,
)


@pytest.fixture
def stores() -> tuple[InMemorySessionStore, InMemoryTripStore]:
    return InMemorySessionStore(), InMemoryTripStore()


def auth_header(user_id: int = 1) -> dict[str, str]:
    """M9：给测试客户端注入 `Authorization: Bearer` 头。

    🔴 内存 store 的隔离靠 `WHERE user_id` 语义，token 里的 uid 不需要
    真的在 users 表里（生产里 JWT 校验也不查库 —— 见 deps.py 注释）。
    所以默认 uid=1 的 token 就能覆盖 M5~M8 全部测试，行为与"常量 1"时代一致。
    """
    token, _ = create_token(user_id)
    return {"Authorization": f"Bearer {token}"}


def _apply_auth(c: TestClient, user_id: int = 1) -> TestClient:
    c.headers.update(auth_header(user_id))
    return c


@pytest.fixture
def user_store() -> InMemoryUserStore:
    return InMemoryUserStore()


@pytest.fixture
def social_stores() -> dict[str, object]:
    """M10/M11 的四个内存替身，测试可直接操作/断言。"""
    return {
        "guides": InMemoryGuideStore(),
        "comments": InMemoryCommentStore(),
        "likes": InMemoryLikeStore(),
        "favorites": InMemoryFavoriteStore(),
    }


@pytest.fixture
def client(stores, user_store, social_stores) -> TestClient:
    """每个测试一个干净的应用实例（存储与限流计数都从零开始）。"""
    app = create_app(
        session_repo=stores[0],
        trip_repo=stores[1],
        user_repo=user_store,
        guide_repo=social_stores["guides"],
        comment_repo=social_stores["comments"],
        like_repo=social_stores["likes"],
        favorite_repo=social_stores["favorites"],
        limiter=SlidingWindowLimiter(),
    )
    return _apply_auth(TestClient(app))


NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)


def make_trip(trip_id: str = "t-1", *, title: str = "成都三日", destination: str = "成都") -> Trip:
    """一份最小的合法 Trip —— trips 接口测试用。"""
    return Trip(
        trip_id=trip_id,
        title=title,
        destination=destination,
        source="generated",
        created_at=NOW,
        updated_at=NOW,
        summary=TripSummary(total_distance_km=10.5, stop_count=6, hard_errors=0, soft_warnings=2),
    )
