"""InMemory store 的行为测试 —— 它是 MySQL 版的**同签名替身**，
签名之外，顺序/隔离这些语义也要对齐（路由测试就建立在语义一致上）。

隔离的断言只在这里做：路由层拿不到"第二个用户"（`get_current_user_id`
恒为 1，api.md 1.1），但 store 层的 WHERE 语义现在就要对 —— M9 换真 JWT
时它就是唯一防线。
"""

from __future__ import annotations

from app.store.memory import InMemorySessionStore, InMemoryTripStore
from conftest import make_trip


def test_session_store_isolates_users():
    store = InMemorySessionStore()
    store.create(user_id=1, title="我的")
    store.create(user_id=2, title="别人的")

    mine, total = store.list(1)
    assert total == 1
    assert mine[0].title == "我的"

    theirs = store.create(user_id=2, title="别人的2")
    assert store.get(user_id=1, session_id=theirs.session_id) is None, "跨用户读取必须拿不到"
    assert store.delete(user_id=1, session_id=theirs.session_id) is False, "跨用户删除必须删不到"


def test_session_store_default_title():
    store = InMemorySessionStore()
    s = store.create(user_id=1)
    assert s.title, "默认标题不能是空串"


def test_trip_store_isolates_users():
    store = InMemoryTripStore()
    store.save(user_id=1, trip=make_trip("t-mine"))
    store.save(user_id=2, trip=make_trip("t-theirs"))

    assert store.get(user_id=1, trip_id="t-theirs") is None, "跨用户读取必须拿不到"
    items, total = store.list(user_id=1)
    assert total == 1 and items[0].trip_id == "t-mine"


def test_trip_store_resave_updates_not_duplicates():
    """同一 trip_id 重复 save = 重算覆盖（M7 PATCH 的写路径），不是新插一条。"""
    store = InMemoryTripStore()
    store.save(user_id=1, trip=make_trip("t-1"))
    first_created = store._rows["t-1"]["created_at"]

    updated = make_trip("t-1", title="改名了")
    store.save(user_id=1, trip=updated)

    _, total = store.list(user_id=1)
    assert total == 1, "覆盖，不是重复"
    assert store.get(1, "t-1").title == "改名了"
    assert store._rows["t-1"]["created_at"] == first_created, "created_at 保持首次创建时间"


def test_trip_store_list_projects_summary():
    store = InMemoryTripStore()
    store.save(user_id=1, trip=make_trip("t-1"))

    items, _ = store.list(user_id=1)
    assert items[0].summary.stop_count == 6, "列表项要带 summary（卡面数字）"
