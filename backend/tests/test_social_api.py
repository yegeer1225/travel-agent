"""M10/M11 社区接口测试：guides CRUD + 发布/撤回 + 评论 + 点赞 + 收藏。

全部走内存替身（conftest.client），语义必须与 MySQL 版一致 ——
两边同签名是 D23 纪律，这里测的就是"同签名"这一半。
"""

from __future__ import annotations

import pytest

from tests.conftest import TestClient, auth_header  # noqa: F401


@pytest.fixture
def c(client):
    """简写：已带 uid=1 token 的客户端。"""
    return client


def _as_user(uid: int) -> dict[str, str]:
    return auth_header(user_id=uid)


def _mk_guide(c, *, title="成都两日游记", content=" day1 宽窄巷子 " * 5, **kw):
    body = {"title": title, "content_md": content, **kw}
    return c.post("/api/guides", json=body)


# ── 攻略 CRUD ──────────────────────────────────────────────


def test_create_guide_defaults_private(c):
    r = _mk_guide(c)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["visibility"] == "private"
    assert data["published_at"] is None
    assert data["author_type"] == "user"
    assert data["summary"].startswith("day1")


def test_public_list_hides_private_and_shows_published(c):
    gid = _mk_guide(c).json()["guide_id"]
    assert c.get("/api/guides").json()["total"] == 0  # private 不进公共列表
    assert c.post(f"/api/guides/{gid}/publish").status_code == 200
    lst = c.get("/api/guides").json()
    assert lst["total"] == 1
    assert lst["items"][0]["published_at"] is not None


def test_private_detail_hidden_from_others(c):
    gid = _mk_guide(c).json()["guide_id"]
    # 匿名 → 404（不是 401：public 直通路径对 private 一律"不存在"）
    bare = TestClient(c.app)
    assert bare.get(f"/api/guides/{gid}").status_code == 404
    # 别的登录用户 → 404
    r = TestClient(c.app).get(f"/api/guides/{gid}", headers=_as_user(2))
    assert r.status_code == 404
    # 本人 → 200
    assert c.get(f"/api/guides/{gid}").status_code == 200


def test_unpublish_removes_from_public(c):
    gid = _mk_guide(c).json()["guide_id"]
    c.post(f"/api/guides/{gid}/publish")
    c.post(f"/api/guides/{gid}/unpublish")
    assert c.get("/api/guides").json()["total"] == 0
    assert c.get(f"/api/guides/{gid}").json()["published_at"] is None


def test_patch_only_updates_given_fields(c):
    gid = _mk_guide(c, destination="成都").json()["guide_id"]
    r = c.patch(f"/api/guides/{gid}", json={"title": "新标题"})
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "新标题"
    assert data["destination"] == "成都"  # 未传字段不动


def test_delete_guide_only_by_owner(c):
    gid = _mk_guide(c).json()["guide_id"]
    other = TestClient(c.app)
    assert other.delete(f"/api/guides/{gid}", headers=_as_user(2)).status_code == 404
    assert c.delete(f"/api/guides/{gid}").status_code == 204
    assert c.get(f"/api/guides/{gid}").status_code == 404


def test_mine_requires_auth_and_returns_all_visibility(c):
    _mk_guide(c)  # private
    gid = _mk_guide(c).json()["guide_id"]
    c.post(f"/api/guides/{gid}/publish")
    mine = c.get("/api/guides?mine=1").json()
    assert mine["total"] == 2  # 自己的 private + public 都在
    bare = TestClient(c.app)
    assert bare.get("/api/guides?mine=1").status_code == 401


def test_create_guide_empty_title_400(c):
    assert _mk_guide(c, title="   ").status_code == 400


# ── 评论 ───────────────────────────────────────────────────


def test_comment_flow_is_mine_and_visibility(c):
    gid = _mk_guide(c).json()["guide_id"]
    c.post(f"/api/guides/{gid}/publish")

    # 用户 2 发评论
    author = TestClient(c.app)
    r = author.post(
        f"/api/guides/{gid}/comments", json={"content": "很实用！"}, headers=_as_user(2)
    )
    assert r.status_code == 201, r.text
    cid = r.json()["comment_id"]
    assert r.json()["is_mine"] is True

    # 匿名可见（guide 已 public）但 is_mine=False
    anon = TestClient(c.app).get(f"/api/guides/{gid}/comments").json()
    assert anon["total"] == 1
    assert anon["items"][0]["is_mine"] is False

    # 作者删自己的评论 → 204
    assert author.delete(f"/api/comments/{cid}", headers=_as_user(2)).status_code == 204


def test_guide_owner_can_delete_others_comment(c):
    gid = _mk_guide(c).json()["guide_id"]
    c.post(f"/api/guides/{gid}/publish")
    r = TestClient(c.app).post(
        f"/api/guides/{gid}/comments", json={"content": "广告"}, headers=_as_user(3)
    )
    cid = r.json()["comment_id"]
    # 路人不能删
    assert TestClient(c.app).delete(f"/api/comments/{cid}", headers=_as_user(4)).status_code == 404
    # 攻略作者（版主语义）能删
    assert c.delete(f"/api/comments/{cid}").status_code == 204


def test_comment_on_private_guide_hidden_from_others(c):
    gid = _mk_guide(c).json()["guide_id"]  # private
    other = TestClient(c.app)
    # 非本人看不到攻略，自然也不能评论 / 看评论
    assert other.post(
        f"/api/guides/{gid}/comments", json={"content": "x"}, headers=_as_user(2)
    ).status_code == 404
    assert other.get(f"/api/guides/{gid}/comments").status_code == 404


# ── 点赞 ───────────────────────────────────────────────────


def test_like_toggle_twice_and_state(c):
    gid = _mk_guide(c).json()["guide_id"]
    c.post(f"/api/guides/{gid}/publish")
    r = c.post("/api/likes/toggle", json={"target_type": "guide", "target_id": gid}).json()
    assert (r["liked"], r["count"]) == (True, 1)
    r = c.post("/api/likes/toggle", json={"target_type": "guide", "target_id": gid}).json()
    assert (r["liked"], r["count"]) == (False, 0)
    state = c.get(f"/api/likes?target_type=guide&target_id={gid}").json()
    assert (state["liked"], state["count"]) == (False, 0)


def test_like_counts_aggregate_across_users(c):
    gid = _mk_guide(c).json()["guide_id"]
    for uid in (1, 2, 3):
        TestClient(c.app).post(
            "/api/likes/toggle", json={"target_type": "guide", "target_id": gid},
            headers=_as_user(uid),
        )
    assert c.get(f"/api/likes?target_type=guide&target_id={gid}").json()["count"] == 3


def test_like_nonexistent_guide_404_but_poi_always_ok(c):
    assert c.post(
        "/api/likes/toggle", json={"target_type": "guide", "target_id": "nope"}
    ).status_code == 404
    # poi 目标的数据在高德侧，我们无法验证存在性 —— 不挡
    assert c.post(
        "/api/likes/toggle", json={"target_type": "poi", "target_id": "B0FFFD3P2C"}
    ).status_code == 200


def test_guide_detail_like_state_reflects_user(c):
    gid = _mk_guide(c).json()["guide_id"]
    c.post(f"/api/guides/{gid}/publish")
    c.post("/api/likes/toggle", json={"target_type": "guide", "target_id": gid})
    assert c.get(f"/api/guides/{gid}").json()["liked"] is True
    assert TestClient(c.app).get(f"/api/guides/{gid}").json()["liked"] is False


# ── 收藏 ───────────────────────────────────────────────────


def test_favorite_poi_requires_name_snapshot(c):
    assert c.post(
        "/api/favorites", json={"target_type": "poi", "target_id": "B0FFFD3P2C"}
    ).status_code == 400
    r = c.post(
        "/api/favorites",
        json={"target_type": "poi", "target_id": "B0FFFD3P2C", "name": "宽窄巷子景区"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "宽窄巷子景区"


def test_favorite_guide_snapshot_from_title(c):
    gid = _mk_guide(c, title="我的攻略标题").json()["guide_id"]
    r = c.post("/api/favorites", json={"target_type": "guide", "target_id": gid, "name": "伪名"})
    assert r.json()["name"] == "我的攻略标题"  # guide 目标以后端为准，忽略前端传名


def test_favorite_list_filter_and_remove(c):
    c.post("/api/favorites", json={"target_type": "poi", "target_id": "P1", "name": "A"})
    gid = _mk_guide(c).json()["guide_id"]  # guide 目标必须真实存在
    c.post("/api/favorites", json={"target_type": "guide", "target_id": gid})
    all_f = c.get("/api/favorites").json()
    assert all_f["total"] == 2
    assert c.get("/api/favorites?target_type=poi").json()["total"] == 1
    assert c.delete("/api/favorites/poi/P1").status_code == 204
    assert c.delete("/api/favorites/poi/P1").status_code == 404  # 再删 = 不存在
    assert c.get("/api/favorites").json()["total"] == 1


def test_favorite_private_guide_hidden_from_others(c):
    gid = _mk_guide(c).json()["guide_id"]  # private
    other = TestClient(c.app)
    # 别的登录用户收藏 → 404（不是 403：不存在=无权）
    assert other.post(
        "/api/favorites", json={"target_type": "guide", "target_id": gid},
        headers=_as_user(2),
    ).status_code == 404
