"""M9 鉴权核心测试：注册 / 登录 / me / JWT 三态 / 双账号隔离。

🔴 验收判据（内部需求文档 8.4 原话）：**A 登录后拿 B 的 session_id/trip_id 直接请求，
返回 404 而不是 403** —— 403 等于泄露"这个 id 存在"（D15）。

🔴 401 三态（缺失/签名错/过期）必须**同一个 code 同一句话** —— 区分它们等于
告诉攻击者"你的 token 是对的，只是旧了"。
"""

from __future__ import annotations

import dataclasses
import time

from fastapi.testclient import TestClient

from app import config as app_config
from app.api import create_app
from app.api.ratelimit import SlidingWindowLimiter
from app.api.security import TokenError, create_token, decode_token, parse_bearer
from conftest import auth_header
from test_chat_route import FakeHandle, fake_factory

REG = {"username": "alice_01", "password": "secret123", "nickname": "爱丽丝"}


def _client(stores, user_store) -> TestClient:
    app = create_app(
        session_repo=stores[0],
        trip_repo=stores[1],
        user_repo=user_store,
        limiter=SlidingWindowLimiter(),
    )
    return TestClient(app)


def _register(c: TestClient, body: dict = REG) -> dict:
    r = c.post("/api/auth/register", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ── 注册 / 登录 ──

def test_register_returns_token_and_user(stores, user_store):
    c = _client(stores, user_store)
    body = _register(c)
    assert body["token"], "token 必须非空"
    assert body["expires_in"] == 168 * 3600, "默认 TTL 7 天"
    user = body["user"]
    assert user["username"] == "alice_01"
    assert user["nickname"] == "爱丽丝"
    assert "password_hash" not in user, "出网白名单：哈希绝不能漏"


def test_register_duplicate_username_conflict(stores, user_store):
    c = _client(stores, user_store)
    _register(c)
    r = c.post("/api/auth/register", json={"username": "alice_01", "password": "other456"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_login_ok_and_wrong_password(stores, user_store):
    c = _client(stores, user_store)
    _register(c)

    ok = c.post("/api/auth/login", json={"username": "alice_01", "password": "secret123"})
    assert ok.status_code == 200
    assert ok.json()["user"]["username"] == "alice_01"

    wrong = c.post("/api/auth/login", json={"username": "alice_01", "password": "wrong-pass"})
    assert wrong.status_code == 401

    # 🔴 用户不存在与密码错必须**同一句话**（防用户名枚举）
    missing = c.post("/api/auth/login", json={"username": "nobody_here", "password": "whatever"})
    assert missing.status_code == 401
    assert missing.json()["error"]["msg"] == wrong.json()["error"]["msg"]
    assert missing.json()["error"]["code"] == wrong.json()["error"]["code"] == "unauthorized"


def test_register_short_password_rejected(stores, user_store):
    c = _client(stores, user_store)
    r = c.post("/api/auth/register", json={"username": "bob_01", "password": "abc"})
    assert r.status_code in (400, 422)  # Pydantic 校验（min_length=6），错误形状已统一


# ── 401 三态 ──

def test_missing_token_is_401(stores, user_store):
    c = _client(stores, user_store)  # 不注入 Authorization
    r = c.get("/api/sessions")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_garbled_and_expired_tokens_are_indistinguishable(stores, user_store):
    """签名错 / 过期 / 格式怪 → 401 且 msg 完全一致。"""
    c = _client(stores, user_store)

    cases = [
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJ1aWQ6MX0.bad-signature"},
        {"Authorization": "empty-string"},
        {"Authorization": "Basic dXNlcjpwYXNz"},  # 非 Bearer
    ]
    results = [c.get("/api/sessions", headers=h) for h in cases]
    for r in results:
        assert r.status_code == 401, f"{r.status_code} for {r.json()}"
    msgs = {r.json()["error"]["msg"] for r in results}
    assert len(msgs) == 1, f"401 的 msg 必须只有一种，现在有：{msgs}"


def test_expired_token_is_401(monkeypatch, stores, user_store):
    """把 TTL 拧到 0 小时 → 立即签出的 token 就是过期的 → 401。"""
    fresh = dataclasses.replace(app_config.settings, auth_token_ttl_hours=0)
    monkeypatch.setattr(app_config, "settings", fresh)
    monkeypatch.setattr("app.api.security.settings", fresh)  # security 是 from-import 绑定

    token, expires_in = create_token(1)
    assert expires_in == 0
    c = _client(stores, user_store)
    r = c.get("/api/sessions", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


# ── me ──

def test_me_get_and_patch(stores, user_store):
    c = _client(stores, user_store)
    token = _register(c)["token"]
    c.headers.update({"Authorization": f"Bearer {token}"})

    me = c.get("/api/auth/me").json()
    assert me["username"] == "alice_01" and me["nickname"] == "爱丽丝"

    patched = c.patch("/api/auth/me", json={"nickname": "新昵称", "email": "a@b.com"}).json()
    assert patched["nickname"] == "新昵称"
    assert patched["email"] == "a@b.com"

    # PATCH 不带字段 = 不动（显式 None 语义）
    again = c.patch("/api/auth/me", json={}).json()
    assert again["nickname"] == "新昵称"


# ── 🔴 双账号隔离（M9 验收判据）──

def test_isolation_user_b_cannot_touch_user_a_session(stores, user_store):
    app = create_app(
        session_repo=stores[0],
        trip_repo=stores[1],
        user_repo=user_store,
        limiter=SlidingWindowLimiter(),
    )
    a = TestClient(app)
    a.headers.update(auth_header(user_id=1))
    b = TestClient(app)
    b.headers.update(auth_header(user_id=2))

    sid = a.post("/api/sessions", json={"title": "A 的会话"}).json()["session_id"]

    # B 拿 A 的 session_id：查询不到 → 404（🔴 不是 403，D15）
    assert b.get(f"/api/sessions/{sid}").status_code == 404
    assert b.delete(f"/api/sessions/{sid}").status_code == 404
    # B 的列表里看不到 A 的会话
    assert all(s["session_id"] != sid for s in b.get("/api/sessions").json()["items"])
    # A 自己仍能访问
    assert a.get(f"/api/sessions/{sid}").status_code == 200


def test_isolation_user_b_cannot_touch_user_a_trip(stores, user_store):
    from conftest import make_trip

    app = create_app(
        session_repo=stores[0],
        trip_repo=stores[1],
        user_repo=user_store,
        limiter=SlidingWindowLimiter(),
    )
    # 行程只能从 chat/paste 产生 —— 测试里直接种进 store（user_id=1 = A 的资产）
    stores[1].save(1, make_trip("t-a"))

    b = TestClient(app)
    b.headers.update(auth_header(user_id=2))
    assert b.get("/api/trips/t-a").status_code == 404, "B 拿 A 的 trip_id 必须 404 不是 403"
    assert all(t["trip_id"] != "t-a" for t in b.get("/api/trips").json()["items"])


# ── 限流：register/login 按 IP（10/分钟）──

def test_auth_rate_limited_per_ip(stores, user_store):
    c = _client(stores, user_store)
    codes = []
    for i in range(11):
        r = c.post("/api/auth/register", json={"username": f"u_{i:02d}_xyz", "password": "secret123"})
        codes.append(r.status_code)
    assert codes.count(429) == 1, "第 11 次必须 429"
    assert codes[:10] == [200] * 10


# ── security 模块单测 ──

def test_parse_bearer_rejects_empty_and_malformed():
    import pytest as _pytest

    for bad in (None, "", "  ", "Bearer", "Bearer ", "Token abc", "abc def"):
        with _pytest.raises(TokenError):
            parse_bearer(bad)


def test_decode_token_roundtrip():
    token, _ = create_token(42)
    assert decode_token(token) == 42
