"""收藏接口（M9 后半）：`GET/POST /favorites`、`DELETE /favorites/{type}/{id}`。

🔴 `name` 是**收藏时刻的快照**（FavoriteRepo 注释），按目标类型三路解析：
- guide：后端从 `guides` 表取标题/封面（private 且非本人 → 404，不泄露存在性）
- poi：没有本地 POI 库（A31），名字只存在于收藏那一瞬 →
  前端从当前 SpotCard 传 `name`（缺了 400）
- comment：后端取评论内容前 50 字
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import get_comment_repo, get_current_user_id, get_favorite_repo, get_guide_repo
from app.api.errors import AppError
from app.schemas import FavoriteCreateRequest, FavoriteItem, Page, TargetType

router = APIRouter(tags=["favorites"])


def _to_item(rec) -> FavoriteItem:
    """内存替身返回 dict、MySQL 版返回 dataclass —— 这里收口成契约模型。"""
    get = (lambda k: rec[k]) if isinstance(rec, dict) else (lambda k: getattr(rec, k))
    return FavoriteItem(
        target_type=get("target_type"),
        target_id=get("target_id"),
        name=get("name"),
        cover=get("cover"),
        created_at=get("created_at"),
    )


def _resolve_snapshot(
    user_id: int,
    body: FavoriteCreateRequest,
    guide_repo,
    comment_repo,
) -> tuple[str, str | None]:
    """按目标类型解析 (name, cover) 快照。"""
    if body.target_type == TargetType.GUIDE:
        guide = guide_repo.get(body.target_id)
        # private 且非本人 → 404（"不存在 = 无权"，与全局 404 语义一致）
        if guide is None:
            raise AppError("not_found", "攻略不存在", 404)
        gget = (lambda k: guide[k]) if isinstance(guide, dict) else (lambda k: getattr(guide, k))
        if gget("visibility") != "public" and gget("user_id") != user_id:
            raise AppError("not_found", "攻略不存在", 404)
        return gget("title"), gget("cover")
    if body.target_type == TargetType.POI:
        if not (body.name or "").strip():
            raise AppError("invalid_param", "收藏景点需要带上名称（从景点卡传 name）", 400)
        return body.name.strip(), body.cover
    # comment
    comment = comment_repo.get(body.target_id)
    if comment is None:
        raise AppError("not_found", "评论不存在", 404)
    cget = (lambda k: comment[k]) if isinstance(comment, dict) else (lambda k: getattr(comment, k))
    return cget("content")[:50], None


@router.get("/favorites", response_model=Page[FavoriteItem])
def list_favorites(
    user_id: int = Depends(get_current_user_id),
    repo=Depends(get_favorite_repo),
    target_type: TargetType | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[FavoriteItem]:
    items, total = repo.list(
        user_id,
        target_type=target_type.value if target_type else None,
        limit=limit,
        offset=offset,
    )
    return Page[FavoriteItem](
        items=[_to_item(r) for r in items], total=total, limit=limit, offset=offset
    )


@router.post("/favorites", response_model=FavoriteItem)
def add_favorite(
    body: FavoriteCreateRequest,
    user_id: int = Depends(get_current_user_id),
    favorite_repo=Depends(get_favorite_repo),
    guide_repo=Depends(get_guide_repo),
    comment_repo=Depends(get_comment_repo),
) -> FavoriteItem:
    name, cover = _resolve_snapshot(user_id, body, guide_repo, comment_repo)
    rec = favorite_repo.add(user_id, body.target_type.value, body.target_id, name, cover)
    return _to_item(rec)


@router.delete("/favorites/{target_type}/{target_id}", status_code=204)
def remove_favorite(
    target_type: TargetType,
    target_id: str,
    user_id: int = Depends(get_current_user_id),
    repo=Depends(get_favorite_repo),
) -> Response:
    if not repo.remove(user_id, target_type.value, target_id):
        raise AppError("not_found", "收藏不存在", 404)
    return Response(status_code=204)
