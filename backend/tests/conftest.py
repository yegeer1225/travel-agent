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
from app.schemas import Trip, TripSummary
from app.store.memory import InMemorySessionStore, InMemoryTripStore


@pytest.fixture
def stores() -> tuple[InMemorySessionStore, InMemoryTripStore]:
    return InMemorySessionStore(), InMemoryTripStore()


@pytest.fixture
def client(stores) -> TestClient:
    """每个测试一个干净的应用实例（存储与限流计数都从零开始）。"""
    app = create_app(
        session_repo=stores[0],
        trip_repo=stores[1],
        limiter=SlidingWindowLimiter(),
    )
    return TestClient(app)


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
