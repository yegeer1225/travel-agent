"""HTTP 接口契约（`docs/api.md` 引用的那批结构）的自检。

和 `test_schemas.py` 的分工：
  · `test_schemas.py`     —— **行程契约**（Trip 内部：汇总一致性 / 三态 / 不编数据）
  · 本文件                —— **接口契约**（请求体能拦住什么 / 响应结构稳不稳 / TS 生成器会不会静默崩）

最后两条测试是写给未来的自己：**「生成器遇到翻译不了的类型必须抛错」**。
静默生成出错误的 TS 比不生成更糟 —— 前端会拿着错类型开发，而报错点在运行时。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import scripts.gen_ts_types as gen
from app import schemas as s

NOW = datetime(2026, 9, 15, 20, 50, 17, tzinfo=timezone.utc)


# ────────────────────────────────────────────────
#  1. 导出面：__all__ 不许有拼错的名字
# ────────────────────────────────────────────────


def test_every_name_in_all_exists() -> None:
    missing = [n for n in s.__all__ if not hasattr(s, n)]
    assert missing == [], f"__all__ 里有不存在的名字：{missing}"


def test_all_has_no_duplicates() -> None:
    dupes = {n for n in s.__all__ if s.__all__.count(n) > 1}
    assert dupes == set(), f"__all__ 里重复：{dupes}"


# ────────────────────────────────────────────────
#  2. 统一外壳：分页与错误体
# ────────────────────────────────────────────────


def test_page_wraps_items_by_type() -> None:
    card = s.SpotCard(poi_id="B001", name="武侯祠", lng=104.05, lat=30.64)
    page = s.Page[s.SpotCard](items=[card], total=1, limit=20, offset=0)

    dumped = page.model_dump()
    assert dumped["total"] == 1
    assert dumped["items"][0]["name"] == "武侯祠"
    # 空 Page 的默认值也要合法（列表接口第一页可能就是空的）
    assert s.Page[s.SpotCard]().model_dump() == {
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
    }


def test_error_body_shape_matches_api_md() -> None:
    body = s.ErrorBody(error=s.ErrorDetail(code="not_found", msg="会话不存在"))
    assert body.model_dump() == {
        "error": {"code": "not_found", "msg": "会话不存在", "detail": None}
    }


# ────────────────────────────────────────────────
#  3. 热启动（粘贴）：会话字段必须能是 None 的每一处都要能是 None
# ────────────────────────────────────────────────


def test_paste_done_event_allows_null_session() -> None:
    """粘贴不建会话（A37）→ done 的 session_id 必须是 None，不能是空字符串冒充。"""
    evt = s.DoneEvent(trip_id="t-1")
    assert evt.session_id is None

    # 反过来：chat 必须给 session_id，写漏了直接报错
    with pytest.raises(ValidationError):
        s.SessionEvent()  # type: ignore[call-arg]


def test_pasted_trip_source_and_null_session() -> None:
    trip = s.Trip(
        trip_id="t-2",
        title="粘贴的行程",
        destination="成都",
        source=s.TripSource.PASTED,
        created_at=NOW,
        updated_at=NOW,
        summary=s.TripSummary(total_distance_km=0, stop_count=0, hard_errors=0, soft_warnings=0),
    )
    assert trip.session_id is None
    assert trip.source == "pasted"


# ────────────────────────────────────────────────
#  4. 请求体护栏：错误的时间格式要在门口就被拦住
# ────────────────────────────────────────────────


def test_trip_op_rejects_loose_time_format() -> None:
    with pytest.raises(ValidationError):
        s.TripOp(op="update_time", day=1, seq=1, arrive="9:00")  # 必须是 09:00

    ok = s.TripOp(op="update_time", day=1, seq=1, arrive="09:00", stay_min=90)
    assert ok.arrive == "09:00"


def test_patch_trip_requires_at_least_one_op() -> None:
    with pytest.raises(ValidationError):
        s.PatchTripRequest(ops=[])


def test_unknown_op_is_rejected() -> None:
    with pytest.raises(ValidationError):
        s.TripOp(op="add_stop", day=1, seq=1)  # type: ignore[arg-type]


def test_chat_request_rejects_empty_message() -> None:
    with pytest.raises(ValidationError):
        s.ChatRequest(message="")


# ────────────────────────────────────────────────
#  5. 绝不外泄：UserOut 是「哪些字段能出网」的白名单
# ────────────────────────────────────────────────


def test_user_out_has_no_password_field() -> None:
    assert "password_hash" not in s.UserOut.model_fields
    assert "password" not in s.UserOut.model_fields

    # 就算有人手贱传进来，extra="forbid" 会直接报错而不是静默丢弃
    with pytest.raises(ValidationError):
        s.UserOut(id=1, username="alice", created_at=NOW, password_hash="x")  # type: ignore[call-arg]


def test_bcrypt_length_limit_is_enforced() -> None:
    """bcrypt 只吃前 72 字节，超了会静默截断 → 必须在契约层挡掉。"""
    with pytest.raises(ValidationError):
        s.RegisterRequest(username="alice", password="x" * 73)


# ────────────────────────────────────────────────
#  6. 多态：收藏 / 点赞 / 评论共用同一套目标类型
# ────────────────────────────────────────────────


def test_target_type_covers_the_three_polymorphic_cases() -> None:
    assert {t.value for t in s.TargetType} == {"guide", "poi", "comment"}
    state = s.LikeState(target_type=s.TargetType.GUIDE, target_id="g-1", liked=True, count=3)
    assert state.model_dump()["count"] == 3


def test_guide_detail_inherits_list_item_fields() -> None:
    """攻略详情 = 列表卡 + 正文。继承写错的话前端列表页会集体崩。"""
    for field in s.GuideListItem.model_fields:
        assert field in s.GuideDetail.model_fields, f"GuideDetail 丢了字段 {field}"


# ────────────────────────────────────────────────
#  7. TS 生成器：不许静默生成错类型
# ────────────────────────────────────────────────


def test_ts_generator_can_translate_every_model() -> None:
    """新增一个 Pydantic 模型、但生成器翻不出来时 → 这条测试红，而不是产出坏 types.ts。"""
    defs = gen.collect_defs()
    assert "Trip" in defs and "Stop" in defs

    for name, schema in defs.items():
        rendered = gen.render(name, schema)  # 翻不出来会抛 NotImplementedError
        assert rendered.startswith(("export interface", "export type")), name


def test_ts_generator_keeps_discriminant_and_null_union() -> None:
    defs = gen.collect_defs()

    # SSE 事件：type 必须是字面量（否则前端无法按 type 收窄）
    assert 'type: "check"' in gen.render("CheckEvent", defs["CheckEvent"])
    # 可空字段必须带 | null，不能被吞成 string
    assert "| null" in gen.render("DoneEvent", defs["DoneEvent"])
    # 三态枚举必须保留三个字面量
    assert gen.render("CheckStatus", defs["CheckStatus"]).count('"') == 6
