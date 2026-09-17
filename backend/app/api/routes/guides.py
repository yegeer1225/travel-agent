"""攻略社区与互动接口（M10 + M11）。

契约要点（api.md 2.6 / A27）：
- **只有 guides 有 visibility**：列表默认只回 public；`mine=1` 回自己的全部；
  详情 public 直通、private 才校验归属（校验不过 = 404，不是 403）
- 创建**默认 private**，发布才写 published_at；unpublish 撤回并清 published_at
- 点赞 toggle 二合一：返回最终状态，并发靠 likes 唯一键兜底
- 删评论：自己的评论 或 自己攻略下的任意评论（版主语义）
- 点赞数/评论数现算（COUNT），不加冗余字段
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.deps import (
    get_comment_repo,
    get_current_user_id,
    get_guide_repo,
    get_like_repo,
    get_user_repo,
)
from app.api.errors import AppError
from app.api.views import attach_counts, guide_detail_view
from app.api.security import decode_token, parse_bearer
from app.schemas import (
    CommentCreateRequest,
    CommentItem,
    GuideDetail,
    GuideListItem,
    GuideUpsertRequest,
    LikeState,
    LikeToggleRequest,
    Page,
    TargetType,
)

router = APIRouter(tags=["guides"])


# ── 公共小件 ────────────────────────────────────────────────


def _optional_user_id(request: Request) -> int | None:
    """匿名可看的路径用：有合法 token 就解析，没有/坏了 → None（不抛 401）。"""
    header = request.headers.get("authorization")
    if not header:
        return None
    try:
        return decode_token(parse_bearer(header))
    except Exception:
        return None



def _check_guide_visible(rec, user_id: int | None) -> None:
    """public 直通；private 只有本人能看（其余 404，不泄露存在性）。"""
    vis = rec["visibility"] if isinstance(rec, dict) else rec.visibility
    owner = rec["user_id"] if isinstance(rec, dict) else rec.user_id
    if vis != "public" and user_id != owner:
        raise AppError("not_found", "攻略不存在", 404)


# ── 攻略 CRUD（M10）────────────────────────────────────────


@router.get("/guides", response_model=Page[GuideListItem])
def list_guides(
    request: Request,
    guide_repo=Depends(get_guide_repo),
    like_repo=Depends(get_like_repo),
    comment_repo=Depends(get_comment_repo),
    user_repo=Depends(get_user_repo),
    city: str | None = Query(default=None),
    keywords: str | None = Query(default=None),
    mine: int = Query(default=0, ge=0, le=1),
    source_trip_id: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[GuideListItem]:
    user_id = _optional_user_id(request)
    if source_trip_id:
        # D62：发布前弹窗的判据（"我在这条行程下发过几篇"）。
        # **强制登录 + 只查自己的** —— 这个答案只可能是"我的"；别人的攻略
        # （哪怕 public）混进来，用户会看到"已发布 3 篇"却一篇都不是自己的。
        if user_id is None:
            raise AppError("unauthorized", "请先登录", 401)
        recs, total = guide_repo.list_by_source_trip(
            user_id, source_trip_id, limit=limit, offset=offset
        )
    elif mine:
        if user_id is None:
            # mine=1 是"我的攻略"，匿名没有"我的" —— 401 而不是空列表（前端该跳登录）
            raise AppError("unauthorized", "请先登录", 401)
        recs, total = guide_repo.list_mine(user_id, limit=limit, offset=offset)
    else:
        recs, total = guide_repo.list_public(city=city, keywords=keywords, limit=limit, offset=offset)
    return Page[GuideListItem](
        items=attach_counts(recs, user_id, like_repo, comment_repo, user_repo),
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/guides", response_model=GuideDetail, status_code=201)
def create_guide(
    body: GuideUpsertRequest,
    user_id: int = Depends(get_current_user_id),
    guide_repo=Depends(get_guide_repo),
    like_repo=Depends(get_like_repo),
    comment_repo=Depends(get_comment_repo),
    user_repo=Depends(get_user_repo),
) -> GuideDetail:
    if not (body.title or "").strip():
        raise AppError("invalid_param", "标题不能为空", 400)
    rec = guide_repo.create(
        user_id,
        title=body.title.strip(),
        content_md=body.content_md or "",
        destination=body.destination,
        cover=body.cover,
        poi_ids=body.poi_ids,
    )
    return guide_detail_view(rec, user_id, like_repo, comment_repo, user_repo)




@router.get("/guides/{guide_id}", response_model=GuideDetail)
def get_guide(
    request: Request,
    guide_id: str,
    guide_repo=Depends(get_guide_repo),
    like_repo=Depends(get_like_repo),
    comment_repo=Depends(get_comment_repo),
    user_repo=Depends(get_user_repo),
) -> GuideDetail:
    user_id = _optional_user_id(request)
    rec = guide_repo.get(guide_id)
    if rec is None:
        raise AppError("not_found", "攻略不存在", 404)
    _check_guide_visible(rec, user_id)
    return guide_detail_view(rec, user_id, like_repo, comment_repo, user_repo)


@router.patch("/guides/{guide_id}", response_model=GuideDetail)
def update_guide(
    guide_id: str,
    body: GuideUpsertRequest,
    user_id: int = Depends(get_current_user_id),
    guide_repo=Depends(get_guide_repo),
    like_repo=Depends(get_like_repo),
    comment_repo=Depends(get_comment_repo),
    user_repo=Depends(get_user_repo),
) -> GuideDetail:
    # exclude_unset：PATCH 只更新显式传入的字段（契约注释）
    fields = body.model_dump(exclude_unset=True)
    rec = guide_repo.update(user_id, guide_id, fields)
    if rec is None:
        raise AppError("not_found", "攻略不存在", 404)
    return guide_detail_view(rec, user_id, like_repo, comment_repo, user_repo)


@router.post("/guides/{guide_id}/publish", response_model=GuideDetail)
def publish_guide(
    guide_id: str,
    user_id: int = Depends(get_current_user_id),
    guide_repo=Depends(get_guide_repo),
    like_repo=Depends(get_like_repo),
    comment_repo=Depends(get_comment_repo),
    user_repo=Depends(get_user_repo),
) -> GuideDetail:
    rec = guide_repo.set_visibility(user_id, guide_id, "public")
    if rec is None:
        raise AppError("not_found", "攻略不存在", 404)
    return guide_detail_view(rec, user_id, like_repo, comment_repo, user_repo)


@router.post("/guides/{guide_id}/unpublish", response_model=GuideDetail)
def unpublish_guide(
    guide_id: str,
    user_id: int = Depends(get_current_user_id),
    guide_repo=Depends(get_guide_repo),
    like_repo=Depends(get_like_repo),
    comment_repo=Depends(get_comment_repo),
    user_repo=Depends(get_user_repo),
) -> GuideDetail:
    rec = guide_repo.set_visibility(user_id, guide_id, "private")
    if rec is None:
        raise AppError("not_found", "攻略不存在", 404)
    return guide_detail_view(rec, user_id, like_repo, comment_repo, user_repo)


@router.delete("/guides/{guide_id}", status_code=204)
def delete_guide(
    guide_id: str,
    user_id: int = Depends(get_current_user_id),
    guide_repo=Depends(get_guide_repo),
) -> Response:
    if not guide_repo.delete(user_id, guide_id):
        raise AppError("not_found", "攻略不存在", 404)
    return Response(status_code=204)


# ── 评论（M11）─────────────────────────────────────────────


def _to_comment_item(rec, user_id: int | None) -> CommentItem:
    get = (lambda k: rec[k]) if isinstance(rec, dict) else (lambda k: getattr(rec, k))
    return CommentItem(
        comment_id=get("id") if isinstance(rec, dict) else rec.comment_id,
        target_type=get("target_type"),
        target_id=get("target_id"),
        author_name=get("author_name"),
        author_type="user",
        content=get("content"),
        created_at=get("created_at"),
        is_mine=user_id is not None and get("user_id") == user_id,
    )


@router.get("/guides/{guide_id}/comments", response_model=Page[CommentItem])
def list_comments(
    request: Request,
    guide_id: str,
    guide_repo=Depends(get_guide_repo),
    comment_repo=Depends(get_comment_repo),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[CommentItem]:
    user_id = _optional_user_id(request)
    rec = guide_repo.get(guide_id)
    if rec is None:
        raise AppError("not_found", "攻略不存在", 404)
    _check_guide_visible(rec, user_id)
    items, total = comment_repo.list(TargetType.GUIDE.value, guide_id, limit=limit, offset=offset)
    return Page[CommentItem](
        items=[_to_comment_item(r, user_id) for r in items], total=total, limit=limit, offset=offset
    )


@router.post("/guides/{guide_id}/comments", response_model=CommentItem, status_code=201)
def create_comment(
    guide_id: str,
    body: CommentCreateRequest,
    user_id: int = Depends(get_current_user_id),
    guide_repo=Depends(get_guide_repo),
    comment_repo=Depends(get_comment_repo),
) -> CommentItem:
    rec = guide_repo.get(guide_id)
    if rec is None:
        raise AppError("not_found", "攻略不存在", 404)
    _check_guide_visible(rec, user_id)
    created = comment_repo.create(user_id, TargetType.GUIDE.value, guide_id, body.content)
    return _to_comment_item(created, user_id)


@router.delete("/comments/{comment_id}", status_code=204)
def delete_comment(
    comment_id: str,
    user_id: int = Depends(get_current_user_id),
    comment_repo=Depends(get_comment_repo),
    guide_repo=Depends(get_guide_repo),
) -> Response:
    comment = comment_repo.get(comment_id)
    if comment is None:
        raise AppError("not_found", "评论不存在", 404)
    get = (lambda k: comment[k]) if isinstance(comment, dict) else (lambda k: getattr(comment, k))
    # 两种人能删：① 评论作者本人 ② 评论目标（攻略）的作者 —— 版主语义
    allowed = get("user_id") == user_id
    if not allowed and get("target_type") == TargetType.GUIDE.value:
        guide = guide_repo.get(get("target_id"))
        allowed = guide is not None and (guide["user_id"] if isinstance(guide, dict) else guide.user_id) == user_id
    if not allowed:
        # 别人的评论 = 对外同样是 404（api.md 404 语义：不区分不存在与无权）
        raise AppError("not_found", "评论不存在", 404)
    comment_repo.delete(comment_id)
    return Response(status_code=204)


# ── 点赞（M11）─────────────────────────────────────────────


@router.post("/likes/toggle", response_model=LikeState)
def toggle_like(
    body: LikeToggleRequest,
    user_id: int = Depends(get_current_user_id),
    like_repo=Depends(get_like_repo),
    guide_repo=Depends(get_guide_repo),
    comment_repo=Depends(get_comment_repo),
) -> LikeState:
    _require_target_exists(body, guide_repo, comment_repo)
    liked, count = like_repo.toggle(user_id, body.target_type.value, body.target_id)
    return LikeState(target_type=body.target_type, target_id=body.target_id, liked=liked, count=count)


def _require_target_exists(body: LikeToggleRequest, guide_repo, comment_repo) -> None:
    """guide/comment 目标必须真实存在（poi 我们无法验证 —— 目标库在高德侧）。"""
    if body.target_type == TargetType.GUIDE:
        if guide_repo.get(body.target_id) is None:
            raise AppError("not_found", "攻略不存在", 404)
    elif body.target_type == TargetType.COMMENT:
        if comment_repo.get(body.target_id) is None:
            raise AppError("not_found", "评论不存在", 404)


@router.get("/likes", response_model=LikeState)
def get_like_state(
    request: Request,
    target_type: TargetType = Query(...),
    target_id: str = Query(...),
    like_repo=Depends(get_like_repo),
    guide_repo=Depends(get_guide_repo),
    comment_repo=Depends(get_comment_repo),
) -> LikeState:
    _require_target_exists(
        LikeToggleRequest(target_type=target_type, target_id=target_id), guide_repo, comment_repo
    )
    user_id = _optional_user_id(request)
    liked, count = like_repo.state(user_id or 0, target_type.value, target_id)
    return LikeState(target_type=target_type, target_id=target_id, liked=liked, count=count)
