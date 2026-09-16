"""行程接口（M5：列表 + 详情）—— 404 语义与列表投影是重点。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import make_trip


def test_empty_trip_list(client: TestClient):
    """M5 没有 chat，trips 天然是空的 —— 空页必须是合法的 Page（第一页就可能为空）。"""
    body = client.get("/api/trips").json()
    assert body == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_trip_list_projects_summary_not_full_trip(client: TestClient, stores):
    """列表项是卡面字段：有 summary，**没有 days**（TripSummaryItem 注释里的缓存边界）。"""
    _, trip_store = stores
    trip_store.save(1, make_trip("t-1"))

    body = client.get("/api/trips").json()
    item = body["items"][0]
    assert item["trip_id"] == "t-1"
    assert item["title"] == "成都三日"
    assert item["destination"] == "成都"
    assert item["source"] == "generated"
    assert item["summary"]["stop_count"] == 6
    assert "days" not in item, "列表页拖完整行程 = 每页 20 份 JSON 的浪费"


def test_trip_detail_returns_full_trip(client: TestClient, stores):
    _, trip_store = stores
    trip_store.save(1, make_trip("t-1"))

    body = client.get("/api/trips/t-1").json()
    assert body["trip_id"] == "t-1"
    assert "days" in body, "详情要有完整结构（前端据此画表格/地图）"
    assert body["summary"]["hard_errors"] == 0


def test_unknown_trip_is_404(client: TestClient):
    r = client.get("/api/trips/no-such-trip")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_trip_list_newest_first(client: TestClient, stores):
    _, trip_store = stores
    trip_store.save(1, make_trip("t-old", title="旧的"))
    trip_store.save(1, make_trip("t-new", title="新的"))

    body = client.get("/api/trips").json()
    assert [i["trip_id"] for i in body["items"]] == ["t-new", "t-old"]
