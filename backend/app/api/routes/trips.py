"""行程接口（M5：列表 + 详情；PATCH / paste / recheck 排 M7，见 api.md 2.3）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user_id, get_trip_repo
from app.api.errors import AppError
from app.schemas import Page, Trip, TripSummaryItem
from app.store.repo import TripRepo

router = APIRouter(tags=["trips"])


@router.get("/trips")
def list_trips(
    user_id: int = Depends(get_current_user_id),
    repo: TripRepo = Depends(get_trip_repo),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[TripSummaryItem]:
    """我的行程列表（按 created_at 倒序）—— 个人中心「我的 AI 路线规划记录」用它。"""
    items, total = repo.list(user_id, limit=limit, offset=offset)
    return Page[TripSummaryItem](items=items, total=total, limit=limit, offset=offset)


@router.get("/trips/{trip_id}")
def get_trip(
    trip_id: str,
    user_id: int = Depends(get_current_user_id),
    repo: TripRepo = Depends(get_trip_repo),
) -> Trip:
    """完整 Trip（含 days / stops / checks / summary）。

    归属校验在 repo 的 WHERE 里（`user_id` 是第一个位置参数，D31 防线）——
    别人的 trip_id 在这里**查询不到**，自然落进 404，不会 403。
    """
    trip = repo.get(user_id, trip_id)
    if trip is None:
        raise AppError("not_found", "行程不存在", 404)
    return trip
