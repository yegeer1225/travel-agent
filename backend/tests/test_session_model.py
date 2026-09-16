"""会话级模型切换（A45/D55/D56）的回归测试。

覆盖四件事：
1. 建会话带 `model` → 落库 + 响应带回（list/get 同源）
2. 建会话带**不存在**的模型 → 400 fail-fast
3. 建会话带**凭据未配**的模型（qwen-plus 无百炼 key）→ 400 fail-fast
4. chat 路由把 `session.model` 透传给 chat_factory（图按模型选）
5. `_build` 对 `supports_thinking=False` 的模型**剥掉 thinking 参数**（不发，
   不是发 disabled）；凭据按 provider 路由
"""

from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from app import config as app_config
from app.api import create_app
from app.api.ratelimit import SlidingWindowLimiter
from app.llm import _build
from conftest import auth_header
from test_chat_route import FakeHandle, fake_factory

MODEL_OK = "deepseek-v4-pro"
MODEL_BAD = "gpt-99-turbo"
MODEL_QWEN = "qwen-plus"


def _client(stores) -> TestClient:
    app = create_app(
        session_repo=stores[0],
        trip_repo=stores[1],
        limiter=SlidingWindowLimiter(),
    )
    c = TestClient(app)
    c.headers.update(auth_header())  # M9：默认用户 uid=1
    return c


# ── 1. 正常路径：model 落库 + 全链路带回 ──

def test_create_session_with_model_persists(stores):
    client = _client(stores)
    r = client.post("/api/sessions", json={"model": MODEL_OK})
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == MODEL_OK

    # get / list 同源（SQL 版与内存版都走这三个查询）
    got = client.get(f"/api/sessions/{body['session_id']}").json()
    assert got["session"]["model"] == MODEL_OK
    listed = client.get("/api/sessions").json()["items"]
    assert listed[0]["model"] == MODEL_OK


def test_create_session_without_model_defaults_none(stores):
    """省略 model = 跟随 .env 默认，响应里是 null（现状行为不变）。"""
    client = _client(stores)
    r = client.post("/api/sessions", json={})
    assert r.status_code == 200
    assert r.json()["model"] is None


# ── 2/3. fail-fast：不存在的模型 / 凭据未配的模型 ──

def test_create_session_unknown_model_rejected(stores):
    client = _client(stores)
    r = client.post("/api/sessions", json={"model": MODEL_BAD})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_param"


def test_create_session_qwen_without_credentials_rejected(stores, monkeypatch):
    """qwen-plus 在 registry 里，但百炼 key 没配 → 仍 400（D56 fail-fast）。"""
    fresh = dataclasses.replace(app_config.settings, llm_bailian_api_key=None)
    monkeypatch.setattr(app_config, "settings", fresh)
    client = _client(stores)
    r = client.post("/api/sessions", json={"model": MODEL_QWEN})
    assert r.status_code == 400


def test_create_session_qwen_with_credentials_ok(stores, monkeypatch):
    """配上百炼 key 后 qwen-plus 立即可选（registry 名单不改代码）。"""
    fresh = dataclasses.replace(app_config.settings, llm_bailian_api_key="sk-fake")
    monkeypatch.setattr(app_config, "settings", fresh)
    client = _client(stores)
    r = client.post("/api/sessions", json={"model": MODEL_QWEN})
    assert r.status_code == 200
    assert r.json()["model"] == MODEL_QWEN


# ── 4. chat 透传：factory 收到的 model == session.model ──

def test_chat_passes_session_model_to_factory(stores):
    seen: list = []
    handle = FakeHandle()
    app = create_app(
        session_repo=stores[0],
        trip_repo=stores[1],
        limiter=SlidingWindowLimiter(),
        chat_factory=fake_factory(handle, seen_models=seen),
    )
    client = TestClient(app)
    client.headers.update(auth_header())
    sid = client.post("/api/sessions", json={"model": MODEL_OK}).json()["session_id"]

    with client.stream("POST", f"/api/sessions/{sid}/chat", json={"message": "成都两日游"}) as r:
        assert r.status_code == 200
        for _ in r.iter_lines():
            pass

    assert seen, "factory 被调用过"
    assert seen[0] == MODEL_OK, "session.model 必须透传给 chat_factory"


# ── 5. _build：provider 路由 + thinking 参数剥离 ──

def test_build_strips_thinking_for_non_thinking_model(monkeypatch):
    """qwen-plus（supports_thinking=False）：连 extra_body 都不发，不是发 disabled。"""
    fresh = dataclasses.replace(app_config.settings, llm_bailian_api_key="sk-fake")
    monkeypatch.setattr(app_config, "settings", fresh)
    monkeypatch.setattr("app.llm.settings", fresh)  # llm.py 是 from-import 的绑定

    llm = _build(MODEL_QWEN, "enabled", temperature=None)  # 传 enabled 也要被剥
    assert llm.model_name == MODEL_QWEN
    assert not llm.extra_body, "supports_thinking=False 的模型连 extra_body 都不该有"


def test_build_keeps_thinking_for_deepseek(monkeypatch):
    fresh = dataclasses.replace(app_config.settings)
    monkeypatch.setattr(app_config, "settings", fresh)
    monkeypatch.setattr("app.llm.settings", fresh)
    llm = _build("deepseek-flash", "enabled", temperature=None)
    assert (llm.extra_body or {}).get("thinking", {}).get("type") == "enabled"


def test_build_routes_bailian_base_url(monkeypatch):
    fresh = dataclasses.replace(
        app_config.settings,
        llm_bailian_api_key="sk-fake",
        llm_bailian_base_url="https://dashscope.example/compatible-mode/v1",
    )
    monkeypatch.setattr(app_config, "settings", fresh)
    monkeypatch.setattr("app.llm.settings", fresh)
    llm = _build(MODEL_QWEN, "disabled", temperature=0)
    assert "dashscope" in str(llm.openai_api_base)
