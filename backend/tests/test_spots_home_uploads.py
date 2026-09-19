"""M9 后半其余接口测试：spots（列表/搜索/详情）、/home、/uploads/avatar、amap-import。

🔴 **2026-09-17 起 `/spots/search` 走收录库（D70）**，provider 只在一处还会被碰到：
**详情回落**（`GET /spots/{poi_id}` 收录库查不到时）。
🔴 **2026-09-19 起 `/home` 也切收录库（后端交接 R1）**——provider 彻底退出读路径，
本文件故意让**收录库（B1/B2）与 provider（B1~B6）是两个不同的集合** ——
否则"搜不到不回落"这条语义根本测不出来（用同一个集合，回落与不回落结果一样）。

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

    def covers(self, city: str | None) -> bool:  # pragma: no cover - 这些用例不判覆盖
        """`AmapProvider` 协议要求（D67-B）。本文件不走工具循环。"""
        return True

    def __init__(self, pois: list[AmapPoi]) -> None:
        self.pois = pois
        self.search_log: list[str] = []
        """记录 provider 被搜过哪些词 —— 用来断言**本地路径零出站**（D70）。"""

    async def search_poi(self, keyword: str, city: str | None = None, limit: int = 10):
        self.search_log.append(keyword)
        hits = [p for p in self.pois if keyword in p.name or keyword in p.alias]
        return hits[:limit]

    async def get_poi(self, poi_id: str):
        return next((p for p in self.pois if p.poi_id == poi_id), None)

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
    """干净的 app + 收录库（B1/B2）+ fake provider（B1~B6）+ uid=1 已登录。

    **两个集合故意不同**：收录库只有 B1/B2，provider 有 B1~B6。
    `client.fake_provider` 让用例能断言"本地路径没碰 provider"（D70）。
    """
    from types import SimpleNamespace

    from app.store.memory import InMemorySpotStore

    provider_pois = [
        _poi("B1", "宽窄巷子景区", ["宽窄巷子", "少城"], ["https://img/1.jpg"]),
        _poi("B2", "成都武侯祠博物馆", ["武侯祠"], ["https://img/2.jpg"]),
        _poi("B3", "成都大熊猫繁育研究基地", ["大熊猫基地"], ["https://img/3.jpg"]),
        _poi("B4", "人民公园", ["人民公园"], ["https://img/4.jpg"]),
        _poi("B5", "锦里古街", ["锦里"]),
        _poi("B6", "成都太古里", ["太古里"]),
    ]
    provider = FakeProvider(provider_pois)

    app = create_app(
        session_repo=InMemorySessionStore(),
        trip_repo=InMemoryTripStore(),
        user_repo=InMemoryUserStore(),
        guide_repo=InMemoryGuideStore(),
        comment_repo=InMemoryCommentStore(),
        like_repo=InMemoryLikeStore(),
        favorite_repo=InMemoryFavoriteStore(),
        # 收录库只放前两条 —— B3~B6 只在 provider 里，用于验证"不回落"与"详情回落"
        spot_repo=InMemorySpotStore(provider_pois[:2]),
        limiter=SlidingWindowLimiter(),
    )
    app.state.nodes = SimpleNamespace(provider=provider)
    client = TestClient(app)
    client.headers.update(auth_header(user_id=1))
    client.fake_provider = provider

    yield client


# ── /spots/search ──────────────────────────────────────────


def test_search_empty_keywords_400(api):
    r = api.get("/api/spots/search", params={"keywords": "   "})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "keyword_required"


def test_search_maps_to_spot_card(api):
    r = api.get("/api/spots/search", params={"keywords": "宽窄巷子"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["source"] == "local"
    assert data["cached"] is False  # 本地路径没有缓存层（D70）
    assert data["total"] == 1
    card = data["items"][0]
    assert card["poi_id"] == "B1"
    assert card["lng"] == 104.05 and card["lat"] == 30.64
    assert card["rating"] == "4.8"
    assert card["photos"] == ["https://img/1.jpg"]


def test_search_hits_alias(api):
    """只出现在别名里的词也要命中（收录库把 alias 存下来就是为了这个）。"""
    r = api.get("/api/spots/search", params={"keywords": "少城"})
    assert r.json()["items"][0]["poi_id"] == "B1"


def test_search_local_miss_does_not_fall_back(api):
    """🔴 D70 的核心语义：收录库里没有 → **空列表**，不静默换成高德/mock 的实时结果。

    「太古里」只在 provider 里（B6）。如果这里返回了 B6，说明有人在 search 里加了回落 ——
    那会让同一页混两种来源，并把"库里确实没有"这条真实路径掩盖掉（D67 同理）。
    """
    r = api.get("/api/spots/search", params={"keywords": "太古里"})
    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0, "source": "local", "cached": False}
    assert api.fake_provider.search_log == [], "本地搜索不该碰 provider（零出站）"


def test_search_paging_and_city_filter(api):
    assert api.get("/api/spots/search", params={"keywords": "成都"}).json()["total"] == 1  # 武侯祠
    # city 用 LIKE 匹配：库里存「成都」、传「成都」命中
    assert api.get(
        "/api/spots/search", params={"keywords": "成都", "city": "成都"}
    ).json()["total"] == 1
    # 换一个城市 → 0 条（不是报错）
    assert api.get(
        "/api/spots/search", params={"keywords": "成都", "city": "杭州"}
    ).json()["items"] == []
    assert api.get("/api/spots/search", params={"keywords": "成都", "limit": 1}).json()["total"] == 1


def test_search_requires_auth(api):
    bare = TestClient(api.app)
    assert bare.get("/api/spots/search", params={"keywords": "宽窄"}).status_code == 401


# ── /spots（列表，2026-09-18）──────────────────────────────


def test_spot_list_returns_library_only(api):
    """列表 = 收录库全量：B1/B2 两条，provider 的 B3~B6 绝不能混进来。"""
    r = api.get("/api/spots")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 2
    assert {s["poi_id"] for s in data["items"]} == {"B1", "B2"}
    # 列表响应没有搜索语义字段（source/cached 是 SpotSearchResponse 的）
    assert "source" not in data
    assert "cached" not in data
    assert api.fake_provider.search_log == [], "列表路径不该碰 provider（零出站）"


def test_spot_list_paging(api):
    data = api.get("/api/spots", params={"limit": 1}).json()
    assert data["total"] == 2
    assert len(data["items"]) == 1


def test_spot_list_requires_auth(api):
    bare = TestClient(api.app)
    assert bare.get("/api/spots").status_code == 401


# ── /spots/{poi_id} ────────────────────────────────────────


def test_get_spot_by_id_from_library(api):
    r = api.get("/api/spots/B1")
    assert r.status_code == 200
    card = r.json()
    assert card["poi_id"] == "B1" and card["name"] == "宽窄巷子景区"
    assert card["lng"] == 104.05  # SpotCard 精简卡，不含 open_time 等内部字段
    assert "open_time" not in card


def test_get_spot_falls_back_to_provider(api):
    """收录库没有、但 provider 有 → 200（**详情回落**，与 search 的"不回落"是两回事：
    这是"按 id 拿一条"，行程/收藏里的 poi_id 可能指向未收录的地点）。"""
    r = api.get("/api/spots/B6")
    assert r.status_code == 200
    assert r.json()["name"] == "成都太古里"


def test_get_spot_unknown_404(api):
    r = api.get("/api/spots/NOT_EXIST")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


# ── /home（R1：数据源 = 收录库，零出站）────────────────────


def test_home_served_from_library(api):
    """🔴 R1：hero/recommended 全部来自收录库，与 /spots 同源同排序；provider 零出站。

    fixture 收录库只有 B1（带图）/B2（带图）→ hero 2 张，recommended 排除 hero 后为空。
    """
    r = api.get("/api/home")
    assert r.status_code == 200, r.text
    data = r.json()
    hero_ids = {h["poi_id"] for h in data["hero"]}
    assert hero_ids == {"B1", "B2"}
    assert all(h["photo"] for h in data["hero"])  # hero 必须带真图
    rec_ids = {s["poi_id"] for s in data["recommended"]}
    assert rec_ids.isdisjoint(hero_ids)  # 契约：两区不重复
    assert api.fake_provider.search_log == [], "/home 不该碰 provider（零出站，R1）"


def test_home_hero_and_recommended_split(api):
    """库变富：5 条（4 带图 + 1 无图）→ hero 3 张全带图，recommended 拿剩下的（含无图卡）。"""
    from app.store.memory import InMemorySpotStore

    api.app.state.spot_repo = InMemorySpotStore(api.fake_provider.pois[:5])
    data = api.get("/api/home").json()
    hero_ids = {h["poi_id"] for h in data["hero"]}
    rec_ids = {s["poi_id"] for s in data["recommended"]}
    assert len(data["hero"]) == 3
    assert all(h["photo"] for h in data["hero"])
    assert len(data["recommended"]) == 2  # 5 - 3
    assert rec_ids.isdisjoint(hero_ids)


def test_home_hero_without_photo_skipped_not_fabricated(api):
    """hero 只收带照片的：库里唯一无图的 B2 不进 hero（A39：没图就不给，不硬凑）。"""
    from app.store.memory import InMemorySpotStore

    no_photo_b2 = _poi("B2", "成都武侯祠博物馆", ["武侯祠"])  # 无照片
    api.app.state.spot_repo = InMemorySpotStore(
        [_poi("B1", "宽窄巷子景区", ["宽窄巷子"], ["https://img/1.jpg"]), no_photo_b2]
    )
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
