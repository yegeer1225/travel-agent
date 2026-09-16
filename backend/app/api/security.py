"""密码哈希与 JWT 签发/校验（M9）。**全项目只有这里碰 bcrypt / pyjwt。**

═══════════════════════════════════════════════════════════════
 三条安全纪律（面试必被追问的点）
═══════════════════════════════════════════════════════════════

1. **JWT 里只有 `uid` + `exp`**（api.md 第一节定死）。昵称/头像这类会变的信息
   不进 token —— token 无状态签出后**改不了**，把昵称放进去，用户改了昵称
   token 里还是旧的，要么忍要么提前过期，两头堵。
2. **401 三态一个说法**：token 缺失 / 签名错 / 过期，对外都是同一句
   "登录已失效" —— 区分它们等于告诉攻击者"你的 token 是对的，只是旧了"。
3. **bcrypt 直接用，不自己加盐**：bcrypt 把盐编进哈希串里（`$2b$12$...`），
   每次 hash 自动新盐，verify 自己比对 —— 这正是"选它而不是 sha256+salt"
   的原因：把"忘记加盐 / 盐存哪"这类人为错误整个消灭。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import settings


class TokenError(Exception):
    """token 缺失 / 格式错 / 签名错 / 过期 —— 调用方一律转 401，不区分原因。"""


def hash_password(password: str) -> str:
    """注册/落库前调用。bcrypt 自动加盐，输出形如 `$2b$12$...`。"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """登录校验。哈希串里带盐和成本因子，verify 全自动。"""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, UnicodeError):
        return False  # 库里存了脏数据时返回"密码错"，不炸 500


def create_token(user_id: int) -> tuple[str, int]:
    """签发 token。返回 `(token, expires_in秒)` —— 前端拿 expires_in 定时清本地。"""
    ttl_seconds = settings.auth_token_ttl_hours * 3600
    now = datetime.now(timezone.utc)
    payload = {
        "uid": user_id,
        "exp": now + timedelta(seconds=ttl_seconds),
        "iat": now,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token, ttl_seconds


def decode_token(token: str) -> int:
    """校验并取 uid。任何失败都抛 `TokenError` —— 调用方转 401，**不区分原因**。"""
    if not token:
        raise TokenError("empty")
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return int(payload["uid"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise TokenError(str(type(exc).__name__)) from exc


def parse_bearer(header_value: str | None) -> str:
    """从 `Authorization: Bearer <token>` 里取 token 部分。

    ⚠️ M5~M8 期间前端带的是**空字符串**（api.md 30 行），也要当成"没登录"。
    """
    if not header_value or not header_value.strip():
        raise TokenError("missing")
    parts = header_value.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise TokenError("malformed")
    return parts[1]
