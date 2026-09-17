"""M12：行程 → 攻略发布链路（D61 幂等 / D62 判据查询 / D63 发布即公开）。

分两层测，边界清楚：
· **渲染器**是纯函数 —— 直接构造 `Trip` 断言 markdown，不碰 HTTP 也不碰库
· **端点**走 TestClient + 内存替身（conftest 的规矩：tests 不连 MySQL）

⚠️ 端点用例用的行程**不带站点**时 `_first_stop_cover` 根本不会去碰 provider
（`poi_id` 为空直接 `None`）—— 只有要测封面时才显式注入 fake provider，
避免测试被"真 provider 懒建"拖去联网。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.schemas import (
    AmapPoi,
    Check,
    Day,
    DayStats,
    Stop,
    Trip,
    TripSummary,
    Weather,
)
from app.services.guide_render import render_guide_from_trip
from app.store.repo import DuplicateIdempotencyKeyError
from tests.conftest import TestClient, auth_header  # noqa: F401


NOW = datetime(2026, 9, 17, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def c(client):
    """简写：已带 uid=1 token 的客户端。"""
    return client


def _as_user(uid: int) -> dict[str, str]:
    return auth_header(user_id=uid)


def _stop(
    seq: int,
    name: str,
    poi_id: str,
    *,
    stay: int = 60,
    arrive: str | None = "09:00",
    leave: str | None = "10:00",
    km: float = 0.0,
    drive: int = 0,
    **kw,
) -> Stop:
    return Stop(
        seq=seq,
        name=name,
        poi_id=poi_id,
        lng=104.04 + seq / 100,
        lat=30.64,
        stay_min=stay,
        arrive=arrive,
        leave=leave,
        from_prev_km=km,
        from_prev_drive_min=drive,
        **kw,
    )


def _rich_trip(trip_id: str = "t-pub") -> Trip:
    """两天三站的完整样本 —— 渲染器测试的主力。"""
    return Trip(
        trip_id=trip_id,
        title="成都两日 · 古迹与熊猫",
        destination="成都",
        source="generated",
        created_at=NOW,
        updated_at=NOW,
        days=[
            Day(
                day=1,
                date=date(2026, 10, 1),
                theme="市区古迹",
                weather=Weather(
                    status="ok", day_weather="晴", day_temp=24, night_temp=16
                ),
                stops=[
                    _stop(1, "武侯祠", "B001", stay=90, arrive="09:00", leave="10:30",
                          match_reason="三国文化，长辈熟"),
                    _stop(2, "锦里", "B002", stay=120, arrive="10:40", leave="12:40",
                          km=0.8, drive=4, cost_per_person=73.0, rating="4.6",
                          open_time="11:00-02:00",
                          checks=[Check(level="soft", code="queue_time", status="fail",
                                        msg="午市排队可能超过 40 分钟")]),
                ],
                day_stats=DayStats(distance_km=5.2, drive_min=18, walk_km=3.1),
            ),
            Day(day=2, theme="熊猫基地", stops=[
                _stop(1, "成都大熊猫繁育研究基地", "B003", stay=180,
                      arrive="08:00", leave="11:00"),
            ]),
        ],
        summary=TripSummary(total_distance_km=38.2, total_cost_per_person=73.0,
                            stop_count=3, hard_errors=0, soft_warnings=1),
    )


# ══════════════════════════════════════════════════════════════
#  一、渲染器（纯函数）
# ══════════════════════════════════════════════════════════════


def test_render_full_trip():
    r = render_guide_from_trip(_rich_trip())
    md = r.content_md

    assert md.startswith("# 成都两日 · 古迹与熊猫\n")
    assert "> 由 AI 路线规划生成 · 2 天 3 站 · 全程 38.2 km · 人均约 ¥73" in md
    assert "## Day 1 · 市区古迹" in md
    assert "日期 2026-10-01" in md
    assert "天气 晴（16~24°C）" in md
    assert "1. **武侯祠**" in md
    assert "09:00–10:30 · 停留 90 分钟" in md
    assert "> 为什么选它：三国文化，长辈熟" in md
    assert "高德 poi_id：B001" in md
    assert "当天：全天 5.2 km · 步行 3.1 km · 车程 18 分钟" in md
    assert "## Day 2 · 熊猫基地" in md

    assert r.poi_ids == ["B001", "B002", "B003"]
    assert r.destination == "成都"


def test_render_does_not_leak_checks():
    """攻略给人看，不是校验报告 —— 站上的软判据文案不能进正文。"""
    md = render_guide_from_trip(_rich_trip()).content_md
    assert "queue_time" not in md
    assert "排队" not in md


def test_render_hides_missing_fields_instead_of_placeholders():
    """缺数据就是缺 —— 不写"暂无"，也不把 None 渲染成字符串。"""
    t = _rich_trip("t-thin")
    t.days = [Day(day=1, stops=[_stop(1, "某地", "B9", stay=30, arrive=None, leave=None)])]
    t.summary = TripSummary(total_distance_km=0, stop_count=1, hard_errors=0, soft_warnings=0)
    t.days[0].day_stats = None
    md = render_guide_from_trip(t).content_md

    for junk in ("暂无", "None", "null", "0.0 km", "车程 0"):
        assert junk not in md
    assert "人均" not in md  # 全行程没有一个 cost → 汇总行不提钱


def test_render_empty_day_is_explicit():
    t = _rich_trip("t-empty")
    t.days = [Day(day=1, stops=[])]
    assert "这一天空着" in render_guide_from_trip(t).content_md


def test_render_clips_overlong_title():
    """`trips.title` 是 200 宽、`guides.title` 只有 100 —— 不收口会撞 Data too long。"""
    t = _rich_trip("t-long")
    t.title = "成" * 150
    r = render_guide_from_trip(t)
    assert len(r.title) == 100
    assert r.title.endswith("…")


def test_render_title_fallback_and_override():
    t = _rich_trip("t-fb")
    t.title = "   "  # 纯空白是 truthy！必须在判断非空之前塌缩（回归守卫）
    assert render_guide_from_trip(t).title == "成都行程"
    assert render_guide_from_trip(t, title="自定义标题").title == "自定义标题"
    assert render_guide_from_trip(t, destination="重庆").destination == "重庆"


def test_render_deduplicates_poi_ids():
    t = _rich_trip("t-dup")
    t.days[1].stops = [_stop(1, "武侯祠", "B001", stay=30)]
    assert render_guide_from_trip(t).poi_ids == ["B001", "B002"]


def test_render_is_deterministic():
    """零 LLM 的可验证判据：同一份行程渲染两次，字节级相同。"""
    t = _rich_trip("t-det")
    assert render_guide_from_trip(t).content_md == render_guide_from_trip(t).content_md


# ══════════════════════════════════════════════════════════════
#  二、端点（TestClient + 内存替身）
# ══════════════════════════════════════════════════════════════


def _publish(c: TestClient, trip_id: str, *, key=None, body=None, uid=None):
    headers = dict(_as_user(uid)) if uid is not None else {}
    if key is not None:
        headers["Idempotency-Key"] = key
    return c.post(
        f"/api/trips/{trip_id}/publish-as-guide",
        json=body if body is not None else {},
        headers=headers,
    )


def _count(c: TestClient, trip_id: str, *, uid=None):
    r = c.get("/api/guides", params={"source_trip_id": trip_id},
              headers=dict(_as_user(uid)) if uid is not None else {})
    return r.status_code, (r.json().get("total") if r.status_code == 200 else None)


def test_publish_creates_public_guide(c, stores):
    stores[1].save(1, _rich_trip())
    r = _publish(c, "t-pub")
    assert r.status_code == 201, r.text
    g = r.json()
    assert g["visibility"] == "public"
    assert g["published_at"] is not None
    assert g["source_trip_id"] == "t-pub"
    assert g["poi_ids"] == ["B001", "B002", "B003"]
    assert g["title"] == "成都两日 · 古迹与熊猫"
    assert "武侯祠" in g["content_md"]
    assert g["cover"] is None  # 没 provider → 拿不到图，但不能因此失败


def test_published_guide_shows_up_in_public_list(c, stores):
    stores[1].save(1, _rich_trip())
    gid = _publish(c, "t-pub").json()["guide_id"]
    pub = c.get("/api/guides").json()
    assert [i["guide_id"] for i in pub["items"]] == [gid]
    assert pub["items"][0]["source_trip_id"] == "t-pub"


def test_publish_other_users_trip_is_404(c, stores):
    stores[1].save(1, _rich_trip())
    assert _publish(c, "t-pub", uid=2).status_code == 404


def test_publish_missing_trip_is_404(c):
    assert _publish(c, "nope").status_code == 404


def test_same_key_returns_same_guide(c, stores):
    """幂等：同 key 重试 → 同一篇，库里不多一条。"""
    stores[1].save(1, _rich_trip())
    a = _publish(c, "t-pub", key="k-1").json()
    b = _publish(c, "t-pub", key="k-1").json()
    assert a["guide_id"] == b["guide_id"]
    assert _count(c, "t-pub") == (200, 1)


def test_new_key_creates_second_guide(c, stores):
    """有意再发一篇 = 新 key → 放行（同一个地方可以有多个行程方案）。"""
    stores[1].save(1, _rich_trip())
    a = _publish(c, "t-pub", key="k-1").json()
    b = _publish(c, "t-pub", key="k-2").json()
    assert a["guide_id"] != b["guide_id"]
    assert _count(c, "t-pub") == (200, 2)


def test_no_key_creates_each_time(c, stores):
    """不带 key（curl / 脚本）→ 照常新建，不强制。"""
    stores[1].save(1, _rich_trip())
    a = _publish(c, "t-pub").json()
    b = _publish(c, "t-pub").json()
    assert a["guide_id"] != b["guide_id"]
    assert _count(c, "t-pub") == (200, 2)


def test_key_length_is_capped(c, stores):
    stores[1].save(1, _rich_trip())
    assert _publish(c, "t-pub", key="x" * 65).status_code == 400
    assert _publish(c, "t-pub", key="y" * 64).status_code == 201


def test_cover_failure_never_blocks_publish(c, stores):
    """封面是装饰：provider 挂了也必须发布成功（缺就隐藏）。"""
    stores[1].save(1, _rich_trip())

    class _Boom:
        async def get_poi(self, poi_id):
            raise RuntimeError("高德挂了")

    c.app.state.nodes = SimpleNamespace(provider=_Boom())
    r = _publish(c, "t-pub")
    assert r.status_code == 201
    assert r.json()["cover"] is None


def test_cover_comes_from_first_stop_photo(c, stores):
    stores[1].save(1, _rich_trip())
    seen: list[str] = []

    class _Fake:
        async def get_poi(self, poi_id):
            seen.append(poi_id)
            return AmapPoi(poi_id=poi_id, name="武侯祠", lng=104.04, lat=30.64,
                           photos=["https://aos-comment.amap.com/a.jpg"])

    c.app.state.nodes = SimpleNamespace(provider=_Fake())
    assert _publish(c, "t-pub").json()["cover"] == "https://aos-comment.amap.com/a.jpg"
    assert seen == ["B001"]  # 只查首站，不打一串高德请求


def test_publish_title_override(c, stores):
    stores[1].save(1, _rich_trip())
    g = _publish(c, "t-pub", body={"title": "我自己的标题", "destination": "成都"}).json()
    assert g["title"] == "我自己的标题"


# ── D62 判据查询的权限面 ────────────────────────────────────


def test_source_trip_filter_requires_login(c, stores):
    stores[1].save(1, _rich_trip())
    _publish(c, "t-pub")
    r = c.get("/api/guides", params={"source_trip_id": "t-pub"},
              headers={"Authorization": ""})
    assert r.status_code == 401


def test_source_trip_filter_is_isolated_between_users(c, stores):
    """别人的攻略（哪怕 public）不该进我的发布判据 —— 否则弹窗会谎报。"""
    stores[1].save(1, _rich_trip("t-a"))
    stores[1].save(2, _rich_trip("t-b"))
    _publish(c, "t-a")
    assert _count(c, "t-a") == (200, 1)
    assert _count(c, "t-a", uid=2) == (200, 0)


# ── 存储层的唯一约束（幂等兜底的那一半）────────────────────


def test_store_rejects_duplicate_idempotency_key(social_stores):
    st = social_stores["guides"]
    st.create(1, title="A", content_md="x", idempotency_key="k")
    with pytest.raises(DuplicateIdempotencyKeyError):
        st.create(1, title="B", content_md="y", idempotency_key="k")

    st.create(2, title="C", content_md="z", idempotency_key="k")  # 换用户互不影响
    st.create(1, title="D", content_md="w")                      # 不带 key 不受约束
    st.create(1, title="E", content_md="v")
    assert len(st._rows) == 4


def test_find_by_idempotency_scoped_to_user(social_stores):
    st = social_stores["guides"]
    rec = st.create(1, title="A", content_md="x", idempotency_key="k")
    assert st.find_by_idempotency(1, "k")["id"] == rec["id"]
    assert st.find_by_idempotency(2, "k") is None
    assert st.find_by_idempotency(1, "none") is None
