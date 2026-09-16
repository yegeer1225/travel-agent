"""两个 repo：sessions / trips 的读写。**薄 SQL，查询结果直接喂 Pydantic**。

═══════════════════════════════════════════════════════════════
 三条纪律（违反任何一条 = 静默泄露或静默丢写）
═══════════════════════════════════════════════════════════════

**① 每个方法的第一个位置参数必须是 `user_id`。**
   这是 D31「隔离靠结构不靠自觉」在存储层的落地：
   所有 WHERE 都写在方法体内、且只能从 `user_id` 参数来 ——
   调用方想查别人的数据，必须显式传别人的 user_id，这个动作
   在 code review 里一眼可见。对比"每个调用点自己拼 WHERE"：
   漏拼一次不会报错，只会把别人的行程吐出去。

**② 拿不到返回 `None` / 空列表，路由层负责转成 404。**
   "不存在"和"无权访问"在存储层就是同一件事（同一个 WHERE 没命中）——
   这正好和 api.md 的 404 语义对齐：两者对外都必须是 404，不许 403。

**③ 短连接。** 每个方法开连接、用完即关（构造函数收的是连接**工厂**）。
   长连接在 FastAPI 里要自己处理 ping/重连，省那点握手钱不值当；
   M6 chat 高频写入时若实测握手成瓶颈，再在这层加连接池，签名不变。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import closing
from typing import Any

import pymysql.connections
import pymysql.cursors

from app.schemas import (
    ChatMessage,
    MessageMeta,
    MessageRole,
    Session,
    Trip,
    TripSource,
    TripSummary,
    TripSummaryItem,
    UserOut,
)
from app.store.db import aware, connect, utc_now

DEFAULT_SESSION_TITLE = "新的行程规划"

ConnectionFactory = Callable[[], pymysql.connections.Connection]


class SessionRepo:
    """`sessions` 表。M5 只做 CRUD；`append_message` 等 M6 接 chat 时再加。"""

    def __init__(self, conn_factory: ConnectionFactory | None = None) -> None:
        self._conn_factory = conn_factory or connect

    def create(
        self,
        user_id: int,
        title: str | None = None,
        *,
        session_id: str | None = None,
        model: str | None = None,
    ) -> Session:
        now = utc_now()
        sid = session_id or uuid.uuid4().hex
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (id, user_id, title, model, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (sid, user_id, title or DEFAULT_SESSION_TITLE, model, now, now),
            )
        return Session(session_id=sid, title=title or DEFAULT_SESSION_TITLE, model=model, created_at=aware(now), updated_at=aware(now))  # type: ignore[arg-type]

    def list(self, user_id: int, *, limit: int = 20, offset: int = 0) -> tuple[list[Session], int]:
        """列表（按 updated_at 倒序）+ 满足条件的总数（分页外壳的 total 用）。"""
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM sessions WHERE user_id = %s", (user_id,))
            total = int(cur.fetchone()["n"])
            cur.execute(
                "SELECT id, title, model, created_at, updated_at FROM sessions "
                "WHERE user_id = %s ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                (user_id, limit, offset),
            )
            rows = cur.fetchall()
        items = [
            Session(
                session_id=r["id"], title=r["title"], model=r["model"],
                created_at=aware(r["created_at"]), updated_at=aware(r["updated_at"]),
            )
            for r in rows
        ]
        return items, total

    def get(self, user_id: int, session_id: str) -> Session | None:
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, model, created_at, updated_at FROM sessions "
                "WHERE user_id = %s AND id = %s",
                (user_id, session_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Session(
            session_id=row["id"], title=row["title"], model=row["model"],
            created_at=aware(row["created_at"]), updated_at=aware(row["updated_at"]),
        )

    def delete(self, user_id: int, session_id: str) -> bool:
        """删会话。⚠️ **不删行程**（api.md 2.2：行程是独立资产，个人中心还要列）。"""
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            n = cur.execute(
                "DELETE FROM sessions WHERE user_id = %s AND id = %s",
                (user_id, session_id),
            )
        return n > 0

    # ══════════ M6：消息（chat 的落库面） ══════════

    def append_message(
        self,
        user_id: int,
        session_id: str,
        *,
        role: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> ChatMessage:
        """存一条消息并 touch 会话的 updated_at（列表按它倒序 —— 聊过的会话浮上来）。

        归属校验靠先查会话（`user_id` 进 WHERE）：会话不存在 / 不是你的，
        这里直接 404，**绝不会往别人的会话里写消息**。
        """
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM sessions WHERE user_id = %s AND id = %s", (user_id, session_id))
            if cur.fetchone() is None:
                raise KeyError(session_id)  # 路由层转 404（统一 not_found）
            now = utc_now()
            mid = uuid.uuid4().hex
            cur.execute(
                "INSERT INTO messages (id, session_id, role, content, meta_json, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (mid, session_id, role, content, json.dumps(meta, ensure_ascii=False) if meta else None, now),
            )
            cur.execute("UPDATE sessions SET updated_at = %s WHERE id = %s", (now, session_id))
        return ChatMessage(
            id=mid, role=MessageRole(role), content=content,
            meta=MessageMeta.model_validate(meta) if meta else None,
            created_at=aware(now),  # type: ignore[arg-type]
        )

    def list_messages(self, user_id: int, session_id: str) -> list[ChatMessage]:
        """会话的全部消息（按时间正序 —— 对话是从上往下读的）。"""
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM sessions WHERE user_id = %s AND id = %s", (user_id, session_id))
            if cur.fetchone() is None:
                raise KeyError(session_id)
            cur.execute(
                "SELECT id, role, content, meta_json, created_at FROM messages "
                "WHERE session_id = %s ORDER BY created_at ASC, id ASC",
                (session_id,),
            )
            rows = cur.fetchall()
        items: list[ChatMessage] = []
        for r in rows:
            meta_raw = _loads(r["meta_json"]) if r["meta_json"] else None
            items.append(
                ChatMessage(
                    id=r["id"], role=MessageRole(r["role"]), content=r["content"],
                    meta=MessageMeta.model_validate(meta_raw) if meta_raw else None,
                    created_at=aware(r["created_at"]),  # type: ignore[arg-type]
                )
            )
        return items

    def update_title(self, user_id: int, session_id: str, title: str) -> None:
        """改标题。M6 首轮生成行程后，把「新的行程规划」换成「成都 · 3 天」。"""
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET title = %s, updated_at = %s "
                "WHERE user_id = %s AND id = %s",
                (title[:100], utc_now(), user_id, session_id),
            )


class TripRepo:
    """`trips` 表。行程 JSON 整存整取 —— PATCH 的确定性重算在 M7 是"读出 → 改 → 存回"。"""

    def __init__(self, conn_factory: ConnectionFactory | None = None) -> None:
        self._conn_factory = conn_factory or connect

    def save(self, user_id: int, trip: Trip) -> None:
        """整份行程落库。`trip_id` 冲突 = 同一行程重复生成，直接覆盖（重算就是覆盖）。"""
        now = utc_now()
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO trips (id, session_id, user_id, title, destination, source, trip_json, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE title=VALUES(title), trip_json=VALUES(trip_json), updated_at=VALUES(updated_at)",
                (
                    trip.trip_id, trip.session_id, user_id, trip.title, trip.destination,
                    str(trip.source), trip.model_dump_json(), now, now,
                ),
            )

    def list(self, user_id: int, *, limit: int = 20, offset: int = 0) -> tuple[list[TripSummaryItem], int]:
        """列表只挑**卡面字段** —— 不拖 `trip_json`（列表页不需要完整行程，见 TripSummaryItem 注释）。"""
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM trips WHERE user_id = %s", (user_id,))
            total = int(cur.fetchone()["n"])
            cur.execute(
                "SELECT id, session_id, title, destination, source, created_at, updated_at, trip_json "
                "FROM trips WHERE user_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (user_id, limit, offset),
            )
            rows = cur.fetchall()
        items = []
        for r in rows:
            full = _loads(r["trip_json"])
            items.append(
                TripSummaryItem(
                    trip_id=r["id"],
                    session_id=r["session_id"],
                    title=r["title"],
                    destination=r["destination"],
                    source=TripSource(r["source"]),
                    created_at=aware(r["created_at"]),  # type: ignore[arg-type]
                    updated_at=aware(r["updated_at"]),  # type: ignore[arg-type]
                    summary=TripSummary.model_validate(full["summary"]),
                )
            )
        return items, total

    def get(self, user_id: int, trip_id: str) -> Trip | None:
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT trip_json FROM trips WHERE user_id = %s AND id = %s",
                (user_id, trip_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Trip.model_validate(_loads(row["trip_json"]))


def _loads(value: Any) -> dict:
    """MySQL JSON 列经 PyMySQL 读回来是 str（MariaDB/驱动差异下也可能是 dict）。"""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    return value


class UserRecord:
    """users 表的行对象。**含 password_hash，绝不能直接出网** —— 出网走 `to_out()`。"""

    __slots__ = ("id", "username", "password_hash", "nickname", "email", "avatar", "created_at")

    def __init__(
        self,
        id: int,
        username: str,
        password_hash: str,
        nickname: str | None,
        email: str | None,
        avatar: str | None,
        created_at: Any,
    ) -> None:
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.nickname = nickname
        self.email = email
        self.avatar = avatar
        self.created_at = created_at

    def to_out(self) -> "UserOut":
        """出网白名单：显式挑字段构造 `UserOut`，想多带一个字段都得来这里改。"""
        return UserOut(
            id=self.id,
            username=self.username,
            nickname=self.nickname,
            email=self.email,
            avatar=self.avatar,
            created_at=aware(self.created_at) or utc_now(),
        )


class UserRepo:
    """`users` 表（M9）。注册/登录/me 三件事的存储面。

    与 SessionRepo/TripRepo 同款纪律：`user_id`/`id` 都是第一个位置参数（D31），
    查不到返回 None，路由层转 404/401。
    """

    def __init__(self, conn_factory: ConnectionFactory | None = None) -> None:
        self._conn_factory = conn_factory or connect

    def create(
        self,
        username: str,
        password_hash: str,
        *,
        nickname: str | None = None,
    ) -> UserRecord:
        now = utc_now()
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, nickname, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (username, password_hash, nickname, now),
            )
            uid = cur.lastrowid
        return UserRecord(
            id=int(uid), username=username, password_hash=password_hash,
            nickname=nickname, email=None, avatar=None, created_at=now,
        )

    def get_by_username(self, username: str) -> UserRecord | None:
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, password_hash, nickname, email, avatar, created_at "
                "FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return UserRecord(
            id=int(row["id"]), username=row["username"], password_hash=row["password_hash"],
            nickname=row["nickname"], email=row["email"], avatar=row["avatar"],
            created_at=row["created_at"],
        )

    def get_by_id(self, user_id: int) -> UserRecord | None:
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, password_hash, nickname, email, avatar, created_at "
                "FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return UserRecord(
            id=int(row["id"]), username=row["username"], password_hash=row["password_hash"],
            nickname=row["nickname"], email=row["email"], avatar=row["avatar"],
            created_at=row["created_at"],
        )

    def update_profile(
        self,
        user_id: int,
        *,
        nickname: str | None = None,
        email: str | None = None,
        avatar: str | None = None,
    ) -> UserRecord | None:
        """只更新显式传入的字段（None = 不动）。改完回读，拿不到 = 用户没了。"""
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET "
                "nickname = COALESCE(%s, nickname), "
                "email = COALESCE(%s, email), "
                "avatar = COALESCE(%s, avatar) "
                "WHERE id = %s",
                (nickname, email, avatar, user_id),
            )
        return self.get_by_id(user_id)


__all__ = ["SessionRepo", "TripRepo", "UserRepo", "UserRecord", "DEFAULT_SESSION_TITLE"]
