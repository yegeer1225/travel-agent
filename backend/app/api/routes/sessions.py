"""会话接口（M5：CRUD；`/chat` SSE 排 M6，见 api.md 2.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import get_current_user_id, get_session_repo
from app.api.errors import AppError
from app.schemas import Page, Session, SessionCreateRequest, SessionDetail
from app.store.repo import SessionRepo

router = APIRouter(tags=["sessions"])


@router.get("/sessions")
def list_sessions(
    user_id: int = Depends(get_current_user_id),
    repo: SessionRepo = Depends(get_session_repo),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[Session]:
    """我的会话列表（按 updated_at 倒序；total = 满足条件的总数）。"""
    items, total = repo.list(user_id, limit=limit, offset=offset)
    return Page[Session](items=items, total=total, limit=limit, offset=offset)


@router.post("/sessions")
def create_session(
    body: SessionCreateRequest = SessionCreateRequest(),
    user_id: int = Depends(get_current_user_id),
    repo: SessionRepo = Depends(get_session_repo),
) -> Session:
    """新建空会话。body 可省（标题空缺是常态 —— 用户第一句话还没说）。"""
    return repo.create(user_id, body.title)


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    user_id: int = Depends(get_current_user_id),
    repo: SessionRepo = Depends(get_session_repo),
) -> SessionDetail:
    """会话 + 全部历史消息。

    ⚠️ M5 阶段 `messages` 恒为空 —— 消息由 M6 的 chat 落库。
    但路由现在就把 404 语义、归属校验做对（见 `_get_owned`），
    M6 接消息时这里只加一行查询。
    """
    session = repo.get(user_id, session_id)
    if session is None:
        # 🔴 404 语义（api.md 1.2）：不存在 = 无权访问，对外一律 not_found，不许 403
        raise AppError("not_found", "会话不存在", 404)
    return SessionDetail(session=session, messages=[])


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: str,
    user_id: int = Depends(get_current_user_id),
    repo: SessionRepo = Depends(get_session_repo),
) -> Response:
    """删会话。**不删行程** —— 行程是独立资产，个人中心的列表不依赖会话存在。"""
    if not repo.delete(user_id, session_id):
        raise AppError("not_found", "会话不存在", 404)
    return Response(status_code=204)
