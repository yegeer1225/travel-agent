"""错误响应契约（api.md 1.2 / 六）：**所有非 2xx 都是同一个形状**。

FastAPI 默认的 `{"detail": ...}` 必须被改写 —— 这是最容易漏的一处，
漏了它前端会同时面对两种错误格式。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.ratelimit import GLOBAL_IP, SlidingWindowLimiter
from conftest import auth_header, make_trip
from fakes_store import BoomTripStore


def test_validation_error_becomes_invalid_param(client: TestClient):
    """非法请求体（Pydantic 门口拦下）→ 400 + `invalid_param`，不是默认的 422。"""
    r = client.post("/api/sessions", json={"title": 12345})
    assert r.status_code == 400
    body = r.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "invalid_param"
    assert "msg" in body["error"]


def test_app_error_404_shape(client: TestClient):
    """我们的 AppError 原样成 ErrorBody；不存在与无权访问**同码 404**（api.md 1.2）。"""
    r = client.get("/api/sessions/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    assert r.json()["error"]["msg"] == "会话不存在"


def test_unknown_route_also_uses_error_body(client: TestClient):
    """路径不存在（Starlette 自己抛的）也要被改写成 ErrorBody。"""
    r = client.get("/api/definitely-not-a-route")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_rate_limited_response_contract(client: TestClient, stores):
    """🔴 429 的三件套：状态码 + `Retry-After` 头 + `detail.retry_after`。

    前端倒计时读的是 **body 里的**（fetch 拿头要 CORS expose，不可靠）——
    所以 detail 里那个数字缺了 = 前端没法倒计时 = 契约违约。
    """
    session_store, _ = stores
    limiter = SlidingWindowLimiter()
    # TestClient 的来源 IP 是 "testclient" —— 直接把它的窗口填满
    key = f"{GLOBAL_IP.name}:testclient"
    for _ in range(GLOBAL_IP.max_requests):
        limiter.check(key, GLOBAL_IP)

    app = create_app(session_repo=session_store, trip_repo=stores[1], limiter=limiter)
    c = TestClient(app)

    r = c.get("/api/health")
    assert r.status_code == 429
    assert r.headers.get("retry-after", "").isdigit(), "Retry-After 头必须有（网关/浏览器看）"
    error = r.json()["error"]
    assert error["code"] == "rate_limited"
    assert isinstance(error["detail"]["retry_after"], int), "前端倒计时的主路径"
    assert error["detail"]["retry_after"] >= 1


def test_unexpected_exception_becomes_internal_error(stores):
    """🔴 未预期异常 → 500 + `internal_error`，**异常文本不外泄**。

    用一个"一查就炸"的 trip store：数据库挂了是真实会发生的事。
    `raise_server_exceptions=False` 让 TestClient 不把异常直接抛出来，
    而是走完真正的 handler。
    """
    _, trip_store = stores
    app = create_app(
        session_repo=stores[0],
        trip_repo=BoomTripStore(trip_store),
        limiter=SlidingWindowLimiter(),
    )
    c = TestClient(app, raise_server_exceptions=False)
    c.headers.update(auth_header())  # M9：先过鉴权，这条测的是 500 转换

    r = c.get("/api/trips/t-1")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "internal_error"
    assert "RuntimeError" not in body["error"]["msg"], "异常类名不能漏给客户端"
    assert "DSN" not in str(body), "内部细节（路径/DSN/SQL）不能出现在响应里"


def test_trip_routes_are_alive_with_data(client: TestClient, stores):
    """夹具冒烟：存一份行程后列表/详情都通（后面 trips 测试的公共前提）。"""
    _, trip_store = stores
    trip_store.save(1, make_trip("t-1"))

    r = client.get("/api/trips")
    assert r.status_code == 200
    assert r.json()["total"] == 1
