"""会话接口（M5 CRUD）—— 行为逐条对 api.md 2.2。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_create_session_with_explicit_title(client: TestClient):
    r = client.post("/api/sessions", json={"title": "成都三天"})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "成都三天"
    assert body["session_id"], "session_id 必须由后端生成"
    # 契约：ISO 8601 带时区（DB 是 naive UTC，读出必须补回时区）
    assert "T" in body["created_at"]
    assert body["created_at"].endswith("+00:00") or body["created_at"].endswith("Z")


def test_create_session_with_empty_body_uses_default_title(client: TestClient):
    """body 可省 —— 用户第一句话还没说，标题空缺是常态，不该报错。"""
    r = client.post("/api/sessions")
    assert r.status_code == 200
    assert r.json()["title"], "后端要给默认标题"


def test_list_sessions_newest_first_with_total(client: TestClient):
    """列表倒序（最新在前）+ total 是**满足条件的总数**（不是本页条数）。"""
    ids = [client.post("/api/sessions", json={"title": f"会话{i}"}).json()["session_id"] for i in range(3)]

    r = client.get("/api/sessions")
    body = r.json()
    assert body["total"] == 3
    assert [s["session_id"] for s in body["items"]] == list(reversed(ids)), "最新创建的排最前"

    # 分页外壳字段齐全
    assert set(body) == {"items", "total", "limit", "offset"}


def test_list_sessions_pagination(client: TestClient):
    for i in range(3):
        client.post("/api/sessions", json={"title": f"s{i}"})

    body = client.get("/api/sessions", params={"limit": 2, "offset": 0}).json()
    assert len(body["items"]) == 2
    assert body["total"] == 3, "total 是总数，不是本页条数"

    body2 = client.get("/api/sessions", params={"limit": 2, "offset": 2}).json()
    assert len(body2["items"]) == 1
    assert body["items"][0]["session_id"] != body2["items"][0]["session_id"]


def test_get_session_detail_messages_empty_for_now(client: TestClient):
    sid = client.post("/api/sessions", json={"title": "x"}).json()["session_id"]
    body = client.get(f"/api/sessions/{sid}").json()
    assert body["session"]["session_id"] == sid
    assert body["messages"] == [], "M5 还没有 chat（M6 落库后这里才有消息）"


def test_get_unknown_session_is_404_not_403(client: TestClient):
    """🔴 拿别人的/不存在的 id 请求 → 404。403 等于告诉攻击者"存在但不给你看"。"""
    r = client.get("/api/sessions/no-such-session")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_delete_session_is_idempotent_by_status(client: TestClient):
    sid = client.post("/api/sessions").json()["session_id"]

    assert client.delete(f"/api/sessions/{sid}").status_code == 204
    assert client.delete(f"/api/sessions/{sid}").status_code == 404, "删两次第二次必须 404"

    # 列表也少了
    assert client.get("/api/sessions").json()["total"] == 0


def test_deleting_session_keeps_untouched_sessions(client: TestClient):
    keep = client.post("/api/sessions", json={"title": "留着"}).json()["session_id"]
    gone = client.post("/api/sessions", json={"title": "删掉"}).json()["session_id"]

    client.delete(f"/api/sessions/{gone}")

    r = client.get(f"/api/sessions/{keep}")
    assert r.status_code == 200
