"""M9 后半其余接口测试：spots/search、/home、/uploads/avatar、amap-import。

provider 用 fake（SpotSearchResponse.source 断言 mock 路由）；
上传产物在 teardown 里清掉（Cleanup 铁律）。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.api.main import create_app
from app.api.ratelimit import SlidingWindowLimiter
from app.schemas import AmapPoi
from app.store.memory import (
    InMemoryCommentStore,
    InMemoryFavoriteStore,
    InMemoryGuideStore,
    InMemoryLikeStore,
    InMemorySessionStore,
    InMemoryTripStore,
    InMemoryUserStore,
)
from tests.conftest import auth_header

AVATAR_DIR = Path(__file__).resolve().parents[1] / "uploads" / "avatars"


class FakeProvider:
    """只实现 search_poi 的假 provider；按 name 包含匹配。"""

    name = "mock"

    def __init__(self, pois: list[AmapPoi]) -> None:
        self.pois = pois

    async def search_poi(self, keyword: str, city: str | None = None, limit: int = 10):
        hits = [p for p in self.pois if keyword in p.name or keyword in p.alias]
        return hits[:limit]

    async def get_weather(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    async def calc_distance(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError


def _poi(pid: str, name: str, alias: list[str] | None = None, photos: list[str] | None = None) -> AmapPoi:
    return AmapPoi(
        poi_id=pid, name=name, alias=alias or [], lng=104.05, lat=30.64,
        cityname="成都", adname="青羊区", photos=photos or [],
        rating="4.8", cost_per_person=50.0,
    )


@pytest.fixture
def api(monkeypatch):
    """干净的 app + fake provider + uid=1 已登录。"""
    from types import SimpleNamespace

    app = create_app(
        session_repo=InMemorySessionStore(),
        trip_repo=InMemoryTripStore(),
        user_repo=InMemoryUserStore(),
        guide_repo=InMemoryGuideStore(),
        comment_repo=InMemoryCommentStore(),
        like_repo=InMemoryLikeStore(),
        favorite_repo=InMemoryFavoriteStore(),
        limiter=SlidingWindowLimiter(),
    )
    provider = FakeProvider(
        [
            _poi("B1", "宽窄巷子景区", ["宽窄巷子"], ["https://img/1.jpg"]),
            _poi("B2", "成都武侯祠博物馆", ["武侯祠"], ["https://img/2.jpg"]),
            _poi("B3", "成都大熊猫繁育研究基地", ["大熊猫基地"], ["https://img/3.jpg"]),
            _poi("B4", "人民公园", ["人民公园"], ["https://img/4.jpg"]),
            _poi("B5", "锦里古街", ["锦里"]),
            _poi("B6", "成都太古里", ["太古里"]),
        ]
    )
    app.state.nodes = SimpleNamespace(provider=provider)
    client = TestClient(app)
    client.headers.update(auth_header(user_id=1))
    # 模块级缓存逐测试清零（进程内缓存会串测试）
    from app.api.routes import home as home_mod, spots as spots_mod

    spots_mod._cache.clear()
    monkeypatch.setattr(home_mod, "_home_cache", None)
    yield client
    spots_mod._cache.clear()
    monkeypatch.setattr(home_mod, "_home_cache", None)


# ── /spots/search ──────────────────────────────────────────


def test_search_empty_keywords_400(api):
    r = api.get("/api/spots/search", params={"keywords": "   "})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "keyword_required"


def test_search_maps_to_spot_card(api):
    r = api.get("/api/spots/search", params={"keywords": "宽窄巷子"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["source"] == "mock"
    assert data["cached"] is False
    card = data["items"][0]
    assert card["poi_id"] == "B1"
    assert card["lng"] == 104.05 and card["lat"] == 30.64
    assert card["rating"] == "4.8"
    # 别名也能命中（模糊匹配与封闭世界校验同一套习惯）
    assert api.get("/api/spots/search", params={"keywords": "太古里"}).json()["items"][0]["poi_id"] == "B6"


def test_search_cache_flag(api):
    api.get("/api/spots/search", params={"keywords": "宽窄巷子"})
    r = api.get("/api/spots/search", params={"keywords": "宽窄巷子"})
    assert r.json()["cached"] is True


def test_search_requires_auth(api):
    bare = TestClient(api.app)
    assert bare.get("/api/spots/search", params={"keywords": "宽窄"}).status_code == 401


# ── /home ──────────────────────────────────────────────────


def test_home_hero_and_recommended(api):
    r = api.get("/api/home")
    assert r.status_code == 200, r.text
    data = r.json()
    # 4 个 hero 关键词都有照片 → hero 4 张
    assert len(data["hero"]) == 4
    assert all(h["photo"] for h in data["hero"])
    # recommended 与 hero 不重复
    hero_ids = {h["poi_id"] for h in data["hero"]}
    assert {s["poi_id"] for s in data["recommended"]}.isdisjoint(hero_ids)


def test_home_pois_without_photo_skipped_not_fabricated(api):
    # 4 个 hero 关键词里只有 1 个有照片 → hero 只给 1 张（绝不硬凑/占位图）
    api.app.state.nodes.provider.pois = [
        _poi("B1", "宽窄巷子景区", ["宽窄巷子"], ["https://img/1.jpg"]),
        _poi("B2", "成都武侯祠博物馆", ["武侯祠"]),  # 无照片
        _poi("B3", "成都大熊猫繁育研究基地", ["大熊猫基地"]),
        _poi("B4", "人民公园", ["人民公园"]),
    ]
    data = api.get("/api/home").json()
    assert len(data["hero"]) == 1
    assert data["hero"][0]["poi_id"] == "B1"


# ── /uploads/avatar ────────────────────────────────────────


def _png_bytes(size: tuple[int, int] = (300, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 60, 60)).save(buf, format="PNG")
    return buf.getvalue()


def _webp_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), (10, 10, 200)).save(buf, format="WEBP")
    return buf.getvalue()


_created: list[Path] = []


def _watch_uploads():
    before = set(AVATAR_DIR.glob("*"))
    return lambda: [p.unlink(missing_ok=True) for p in set(AVATAR_DIR.glob("*")) - before]


def test_upload_avatar_png_reencoded_webp(api):
    cleanup = _watch_uploads()
    try:
        r = api.post(
            "/api/uploads/avatar",
            files={"file": ("a.png", _png_bytes((300, 200)), "image/png")},
        )
        assert r.status_code == 200, r.text
        url = r.json()["url"]
        assert url.startswith("/uploads/avatars/") and url.endswith(".webp")
        # 静态服务能取回重编码产物
        got = api.get(url)
        assert got.status_code == 200
        assert got.headers["content-type"] == "image/webp"
        img = Image.open(io.BytesIO(got.content))
        assert img.size == (512, 512)  # D29：强制 512×512
    finally:
        cleanup()


def test_upload_avatar_rejects_fakes_and_gif(api):
    cleanup = _watch_uploads()
    try:
        # 文本伪装 .png —— 魔数嗅探挡掉
        r = api.post(
            "/api/uploads/avatar",
            files={"file": ("evil.png", b"<svg onload=alert(1)>", "image/png")},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "unsupported_format"
        # 空
        r = api.post("/api/uploads/avatar", files={"file": ("e.png", b"", "image/png")})
        assert r.status_code == 400
        # GIF 也不收（D29：不支持 GIF）
        gif = io.BytesIO()
        Image.new("P", (10, 10)).save(gif, format="GIF")
        r = api.post("/api/uploads/avatar", files={"file": ("e.gif", gif.getvalue(), "image/gif")})
        assert r.status_code == 400
    finally:
        cleanup()


def test_upload_avatar_accepts_webp_source(api):
    cleanup = _watch_uploads()
    try:
        r = api.post(
            "/api/uploads/avatar",
            files={"file": ("a.webp", _webp_bytes(), "image/webp")},
        )
        assert r.status_code == 200
    finally:
        cleanup()


def test_upload_avatar_requires_auth(api):
    bare = TestClient(api.app)
    r = bare.post(
        "/api/uploads/avatar", files={"file": ("a.png", _png_bytes(), "image/png")}
    )
    assert r.status_code == 401


# ── amap-import ────────────────────────────────────────────


def _trip_with_stops(trip_id: str):
    from tests.conftest import NOW

    from app.schemas import Day, Stop, Trip, TripSummary

    return Trip(
        trip_id=trip_id, title="成都一日", destination="成都", source="generated",
        created_at=NOW, updated_at=NOW,
        summary=TripSummary(total_distance_km=1, stop_count=2, hard_errors=0, soft_warnings=0),
        days=[
            Day(day=1, stops=[
                Stop(seq=1, name="宽窄巷子景区", poi_id="B1", lng=104.05, lat=30.66, stay_min=60),
                Stop(seq=2, name="人民公园", poi_id="B4", lng=104.06, lat=30.65, stay_min=60),
            ]),
        ],
    )


def test_amap_import_builds_navigation_url(client, stores):
    trip = _trip_with_stops("t-imp")
    stores[1].save(user_id=1, trip=trip)
    r = client.post("/api/trips/t-imp/amap-import")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["url"].startswith("https://uri.amap.com/navigation?")
    assert "104.050000%2C30.660000" in data["url"]
    assert data["note"] is None  # 2 站没有超途经点上限


def test_amap_import_truncates_via_and_notes(client, stores):
    from app.schemas import Day, Stop, Trip, TripSummary
    from tests.conftest import NOW

    stops = [
        Stop(seq=i + 1, name=f"S{i}", poi_id=f"P{i}", lng=104.0 + i * 0.01,
             lat=30.6, stay_min=30)
        for i in range(12)
    ]
    trip = Trip(
        trip_id="t-big", title="大行程", destination="成都", source="generated",
        created_at=NOW, updated_at=NOW,
        summary=TripSummary(total_distance_km=1, stop_count=12, hard_errors=0, soft_warnings=0),
        days=[Day(day=1, stops=stops)],
    )
    stores[1].save(user_id=1, trip=trip)
    r = client.post("/api/trips/t-big/amap-import")
    assert r.status_code == 200
    assert "只带前 9 个" in r.json()["note"]
