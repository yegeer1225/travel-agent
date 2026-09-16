"""鉴权接口（M9）：注册 / 登录 / me 读写。

🔴 三条契约纪律（api.md 第一节 + D57）：
- token 里只有 `uid` + `exp`，昵称/头像**只走 `GET /auth/me`**
- 登录失败与"用户不存在"**同一个 401**（不泄露"这个用户名存在"）
- register/login 限流按 **IP**（攻击者此时还没有账号，按用户限不住）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_user_id, get_limiter, get_user_repo
from app.api.errors import AppError
from app.api.ratelimit import AUTH_PER_MIN
from app.api.errors import RateLimited
from app.api.security import create_token, hash_password, verify_password
from app.schemas import (
    AuthResponse,
    LoginRequest,
    RegisterRequest,
    UpdateProfileRequest,
    UserOut,
)
from app.store.repo import UserRepo

router = APIRouter(prefix="/auth", tags=["auth"])


def _auth_limited(request: Request, limiter) -> None:
    """register/login 共用的 IP 限流（D28：10 / 分钟 · IP）。"""
    retry_after = limiter.check(
        f"{AUTH_PER_MIN.name}:{request.client.host if request.client else 'unknown'}",
        AUTH_PER_MIN,
    )
    if retry_after:
        raise RateLimited(f"操作太频繁，请 {retry_after} 秒后重试", retry_after)


def _issue(user_repo: UserRepo, user) -> AuthResponse:
    token, expires_in = create_token(user.id)
    return AuthResponse(token=token, expires_in=expires_in, user=user.to_out())


@router.post("/register", response_model=AuthResponse)
def register(
    body: RegisterRequest,
    request: Request,
    user_repo: UserRepo = Depends(get_user_repo),
    limiter = Depends(get_limiter),
) -> AuthResponse:
    _auth_limited(request, limiter)
    if user_repo.get_by_username(body.username) is not None:
        # 409 conflict：api.md 错误码表定死"用户名已存在"用 conflict，
        # msg 直接显示在表单下方 —— 这一条**不**算泄露（注册页本来就要提示）
        raise AppError("conflict", "用户名已被占用", 409)
    user = user_repo.create(
        body.username,
        hash_password(body.password),
        nickname=body.nickname or body.username,
    )
    return _issue(user_repo, user)


@router.post("/login", response_model=AuthResponse)
def login(
    body: LoginRequest,
    request: Request,
    user_repo: UserRepo = Depends(get_user_repo),
    limiter = Depends(get_limiter),
) -> AuthResponse:
    _auth_limited(request, limiter)
    user = user_repo.get_by_username(body.username)
    # 🔴 用户不存在与密码错**同一个 401 同一句话** —— 否则等于帮攻击者枚举用户名
    if user is None or not verify_password(body.password, user.password_hash):
        raise AppError("unauthorized", "用户名或密码错误", 401)
    return _issue(user_repo, user)


@router.get("/me", response_model=UserOut)
def me(user_id: int = Depends(get_current_user_id),
       user_repo: UserRepo = Depends(get_user_repo)) -> UserOut:
    user = user_repo.get_by_id(user_id)
    if user is None:
        # token 合法但用户没了（被删）—— 只此一处 404，其他路由 404 由归属查询自然产生
        raise AppError("not_found", "用户不存在", 404)
    return user.to_out()


@router.patch("/me", response_model=UserOut)
def update_me(
    body: UpdateProfileRequest,
    user_id: int = Depends(get_current_user_id),
    user_repo: UserRepo = Depends(get_user_repo),
) -> UserOut:
    user = user_repo.update_profile(
        user_id,
        nickname=body.nickname,
        email=body.email,
        avatar=body.avatar,
    )
    if user is None:
        raise AppError("not_found", "用户不存在", 404)
    return user.to_out()


__all__ = ["router"]
