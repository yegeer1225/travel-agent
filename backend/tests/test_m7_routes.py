"""M7 路由测试：paste（SSE）/ PATCH / recheck。fake nodes 注入，不连库不连 LLM。

paste 的两个契约要点在这里钉死（api.md 3.4）：
- **不发 `session` 帧**、`done.session_id` 为 `null`（A37：不建会话）
- 落库的行程 `source=pasted`、`session_id=None` —— 不能回助手页接着聊
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from conftest import NOW, auth_header, make_trip
from fakes import SOFT_EMPTY, ai_text, make_nodes, soft_says

from app.api.main import create_app
from app.api.ratelimit import PASTE_HOURLY, RECHECK_HOURLY, SlidingWindowLimiter
from app.providers.mock import MOCK_POI_POOL
from app.store.memory import InMemorySessionStore, InMemoryTripStore

BY_NAME = {p.name: p for p in MOCK_POI_POOL}


# ══════════════════════════════════════════════════════════════
#  替身
# ══════════════════════════════════════════════════════════════


def make_client(stores, *, nodes=None, limiter=None) -> TestClient:
    app = create_app(
        session_repo=stores[0],
        trip_repo=stores[1],
        limiter=limiter or SlidingWindowLimiter(),
    )
    if nodes is not None:
        app.state.nodes = nodes
    c = TestClient(app)
    c.headers.update(auth_header())  # M9：默认用户 uid=1
    return c


def paste_plan_json(names: list[str], *, destination: str = "成都") -> str:
    """extract LLM 的脚本输出：站点名 → 地名清单（武侯祠 能命中 mock 池）。"""
    return json.dumps({
        "destination": destination,
        "title": "粘贴来的行程",
        "days": [{"stops": [{"name": n, "stay_min": 90} for n in names]}],
    }, ensure_ascii=False)


def make_client_with_extract(stores, extract_output: str, *, soft=None,
                             limiter: SlidingWindowLimiter | None = None) -> TestClient:
    nodes = make_nodes(extract_script=[ai_text(extract_output), ai_text(extract_output)],
                       soft_script=[soft or SOFT_EMPTY], today=None)
    return make_client(stores, nodes=nodes, limiter=limiter)


def parse(text: str) -> list[dict]:
    frames = []
    for block in text.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if all(ln.startswith(":") for ln in lines):
            frames.append({"ping": True})
            continue
        f: dict = {}
        for ln in lines:
            if ln.startswith("id: "):
                f["id"] = int(ln[4:])
            elif ln.startswith("data: "):
                f["data"] = json.loads(ln[6:])
        frames.append(f)
    return frames


def fill_limit(limiter: SlidingWindowLimiter, rule, key: str, times: int) -> None:
    for _ in range(times):
        limiter.check(f"{rule.name}:{key}", rule)


# ══════════════════════════════════════════════════════════════
#  paste
# ══════════════════════════════════════════════════════════════


def test_paste_happy_path_sse(stores):
    plan = paste_plan_json(["武侯祠", "锦里", "不存在的火星基地"])
    client = make_client_with_extract(stores, plan)
    r = client.post("/api/trips/paste", json={"text": "Day1 上午武侯祠，中午锦里吃小吃", "destination": "成都"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = parse(r.text)
    types = [f["data"]["type"] for f in frames if "data" in f]
    assert "session" not in types, "paste 不发 session 帧（A37）"
    assert types[0] == "node" and "tool_call" in types and "check" in types
    assert types[-1] == "done"

    done = frames[-1]["data"]
    assert done["session_id"] is None, "done.session_id 必须为 null"
    assert done["trip_id"], "成功导入的行程要带回 trip_id"

    # 搜不到的站被跳过并在 token 里提醒
    tokens = [f["data"]["text"] for f in frames if f["data"]["type"] == "token"]
    assert any("没有搜到" in t for t in tokens)

    # 落库：source=pasted、session_id=None
    trip = stores[1].get(1, done["trip_id"])
    assert trip is not None
    assert trip.source.value == "pasted"
    assert trip.session_id is None
    assert trip.destination == "成都"
    assert all(s.name != "不存在的火星基地" for d in trip.days for s in d.stops)


def test_parse_failure_becomes_error_frame(stores):
    client = make_client_with_extract(stores, "这不是 JSON")
    r = client.post("/api/trips/paste", json={"text": "乱七八糟的文本随便粘一段超过十个字"})
    frames = parse(r.text)
    types = [f["data"]["type"] for f in frames if "data" in f]
    assert "error" in types
    error = next(f["data"] for f in frames if f["data"]["type"] == "error")
    assert error["code"] == "paste_parse_failed"
    assert types[-1] == "done", "error 后必发 done（铁律）"


def test_paste_rate_limited(stores):
    limiter = SlidingWindowLimiter()
    fill_limit(limiter, PASTE_HOURLY, "1", PASTE_HOURLY.max_requests)
    client = make_client_with_extract(stores, paste_plan_json(["武侯祠"]), limiter=limiter)
    r = client.post("/api/trips/paste", json={"text": "超过十个字的行程文本", "destination": "成都"})
    assert r.status_code == 429


# ══════════════════════════════════════════════════════════════
#  PATCH
# ══════════════════════════════════════════════════════════════


def _seed_trip(stores) -> str:
    """两天各两站的行程（复用 recompute 测试的构造，坐标来自 mock 池）。"""
    from datetime import date

    from app.schemas import Day, Stop

    def stop(seq, name, arrive, stay=90):
        poi = BY_NAME[name]
        return Stop(seq=seq, name=poi.name, poi_id=poi.poi_id, lng=poi.lng, lat=poi.lat,
                    arrive=arrive, stay_min=stay, leave=None)

    trip = make_trip("t-patch")
    trip = trip.model_copy(update={"days": [
        Day(day=1, date=date(2026, 9, 20), stops=[
            stop(1, "成都武侯祠博物馆", "09:00"), stop(2, "锦里古街", "11:30"),
        ]),
        Day(day=2, date=date(2026, 9, 21), stops=[
            stop(1, "宽窄巷子景区", "09:30"), stop(2, "人民公园", "11:00"),
        ]),
    ]})
    stores[1].save(1, trip)
    return trip.trip_id


def test_patch_moves_and_returns_full_trip(stores):
    tid = _seed_trip(stores)
    nodes = make_nodes(today=None)
    client = make_client(stores, nodes=nodes)

    r = client.patch(f"/api/trips/{tid}", json={"ops": [
        {"op": "move", "day": 1, "seq": 2, "to_day": 2, "to_seq": 1},
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body["trip_id"] == tid
    names_d2 = [s["name"] for s in body["days"][1]["stops"]]
    assert names_d2[0] == "锦里古街", "跨天插入成为目标天新首站"
    assert body["summary"]["stop_count"] == 4

    # 重算真的发生了：第一站 from_prev=0，第二站被 provider 算出
    assert body["days"][0]["stops"][0]["from_prev_km"] == 0
    # 落库
    saved = stores[1].get(1, tid)
    assert [s.name for s in saved.days[1].stops] == names_d2


def test_patch_unknown_trip_is_404(stores):
    client = make_client(stores, nodes=make_nodes(today=None))
    r = client.patch("/api/trips/no-such", json={"ops": [{"op": "delete", "day": 1, "seq": 1}]})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_patch_bad_op_is_400(stores):
    tid = _seed_trip(stores)
    client = make_client(stores, nodes=make_nodes(today=None))
    r = client.patch(f"/api/trips/{tid}", json={"ops": [{"op": "delete", "day": 1, "seq": 99}]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_param"


# ══════════════════════════════════════════════════════════════
#  recheck
# ══════════════════════════════════════════════════════════════


def test_recheck_updates_soft_checks(stores):
    tid = _seed_trip(stores)
    # ⚠️ 用**站级** code（needs_booking 挂 stop，见 LEVEL_OF）——
    # 旧的实现只扫 trip.checks 顶层，站级软 fail 漏进 remaining
    # （2026-09-16 端到端实测抓到的 bug），这条测试钉死它。
    nodes = make_nodes(today=None, soft_script=[soft_says(
        {"code": "needs_booking", "status": "fail", "day": 1, "seq": 1,
         "msg": "武侯祠是热门景点，建议出发前确认一下预约政策"},
    )])
    client = make_client(stores, nodes=nodes)

    r = client.post(f"/api/trips/{tid}/recheck")
    assert r.status_code == 200
    body = r.json()
    assert body["trip_id"] == tid
    assert body["validation"]["rounds"] == 0, "recheck 不重排"
    assert [i["code"] for i in body["validation"]["remaining"]] == ["needs_booking"], \
        "站级软 fail 必须进 remaining（扫全树，不只顶层）"
    soft_in_checks = [c for c in body["checks"] if c["level"] == "soft"]
    assert soft_in_checks, "checks 里带软判据"

    # 落库：下次打开行程还能看到
    saved = stores[1].get(1, tid)
    assert saved.summary.soft_warnings == 1
    assert saved.validation is not None and saved.validation.remaining


def test_recheck_unknown_trip_is_404(stores):
    client = make_client(stores, nodes=make_nodes(today=None))
    r = client.post("/api/trips/no-such/recheck")
    assert r.status_code == 404


def test_recheck_rate_limited(stores):
    tid = _seed_trip(stores)
    limiter = SlidingWindowLimiter()
    fill_limit(limiter, RECHECK_HOURLY, "1", RECHECK_HOURLY.max_requests)
    client = make_client(stores, nodes=make_nodes(today=None), limiter=limiter)
    r = client.post(f"/api/trips/{tid}/recheck")
    assert r.status_code == 429
