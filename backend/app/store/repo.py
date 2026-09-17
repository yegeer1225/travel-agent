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
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pymysql
import pymysql.connections
import pymysql.cursors

from pymysql.err import IntegrityError as MysqlIntegrityError

from app.schemas import (
    AmapPoi,
    ChatMessage,
    MessageMeta,
    MessageRole,
    Session,
    SpotCard,
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


# ══════════════════════════════════════════════════════════════
#  M10/M11：攻略 / 评论 / 点赞 / 收藏
# ══════════════════════════════════════════════════════════════


class DuplicateIdempotencyKeyError(Exception):
    """幂等键撞唯一约束 `uq_guides_idem`（D61）。

    为什么单独造一个异常：真库抛的是 `pymysql.err.IntegrityError`，而内存替身
    没有驱动可抛 —— 两边都翻成这一个类型，**路由层就只需 catch 一种**，
    不必知道底层是 MySQL 还是内存。顺带避开"catch 所有 IntegrityError"那个坑
    （别的完整性问题会被误当幂等冲突吞掉）。
    """


@dataclass
class GuideRecord:
    id: str
    user_id: int
    title: str
    content_md: str
    destination: str | None
    cover: str | None
    poi_ids: list[str]
    visibility: str  # Visibility 值（repo 层不 import schemas 枚举，避免循环）
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
    source_trip_id: str | None = None  # 由哪条行程发布而来（D61）；**只溯源，不加唯一约束**
    idempotency_key: str | None = None  # 那次"发布意图"的身份证（D61）；None = 没带头


class GuideRepo:
    """`guides` 表（M10）。

    ⚠️ 对纪律①的**显式豁免**：`get` / `list_public` 是社区公开读路径
    （A27：只有这张表有 visibility），按设计**不带 user 过滤**；
    一切私有读写仍然走 `*_owned` / `list_mine`（user_id 第一位）。
    """

    def __init__(self, conn_factory: ConnectionFactory | None = None) -> None:
        self._conn_factory = conn_factory or connect

    @staticmethod
    def _row_to_record(row: dict[str, Any]) -> GuideRecord:
        raw = row["poi_ids"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        return GuideRecord(
            id=row["id"], user_id=int(row["user_id"]), title=row["title"],
            content_md=row["content_md"], destination=row["destination"],
            cover=row["cover"], poi_ids=list(raw or []),
            visibility=row["visibility"], published_at=aware(row["published_at"]),
            created_at=aware(row["created_at"]) or utc_now(),
            updated_at=aware(row["updated_at"]) or utc_now(),
            source_trip_id=row.get("source_trip_id"),
            idempotency_key=row.get("idempotency_key"),
        )

    _COLS = (
        "id, user_id, title, content_md, destination, cover, poi_ids, visibility, "
        "published_at, created_at, updated_at, source_trip_id, idempotency_key"
    )

    def create(
        self,
        user_id: int,
        *,
        title: str,
        content_md: str,
        destination: str | None = None,
        cover: str | None = None,
        poi_ids: list[str] | None = None,
        source_trip_id: str | None = None,
        idempotency_key: str | None = None,
        publish: bool = False,
    ) -> GuideRecord:
        """新建攻略。

        `publish=True` → 直接公开（`visibility=public` + `published_at=now`），
        行程发布端点用它（D63）；其余调用方保持默认 private。
        `idempotency_key` 撞 `uq_guides_idem` 会抛 `IntegrityError` ——
        **这一层不吞异常**，由路由层捕获后回查（D61）。
        """
        now = utc_now()
        gid = uuid.uuid4().hex
        visibility = "public" if publish else "private"
        published_at = now if publish else None
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO guides (id, user_id, title, content_md, destination, cover, poi_ids, "
                    "visibility, published_at, source_trip_id, idempotency_key, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (gid, user_id, title, content_md, destination, cover,
                     json.dumps(poi_ids or []), visibility, published_at,
                     source_trip_id, idempotency_key, now, now),
                )
            except MysqlIntegrityError as exc:
                # ⚠️ 只翻译幂等键冲突；别的完整性错误必须原样上抛，
                # 否则"某个 NOT NULL 漏了"会被伪装成"重复发布"。
                if idempotency_key is not None and "uq_guides_idem" in str(exc):
                    raise DuplicateIdempotencyKeyError(str(exc)) from exc
                raise
        return GuideRecord(
            id=gid, user_id=user_id, title=title, content_md=content_md,
            destination=destination, cover=cover, poi_ids=list(poi_ids or []),
            visibility=visibility, published_at=published_at, created_at=now, updated_at=now,
            source_trip_id=source_trip_id, idempotency_key=idempotency_key,
        )

    def get(self, guide_id: str) -> GuideRecord | None:
        """公开读路径（社区详情 public 直通）。⚠️ 刻意不带 user 过滤，见类注释。"""
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS} FROM guides WHERE id = %s", (guide_id,))
            row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def get_owned(self, user_id: int, guide_id: str) -> GuideRecord | None:
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {self._COLS} FROM guides WHERE id = %s AND user_id = %s", (guide_id, user_id))
            row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def list_public(
        self,
        *,
        city: str | None = None,
        keywords: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[GuideRecord], int]:
        cond, params = "visibility = 'public'", []
        if city:
            cond += " AND destination LIKE %s"
            params.append(f"%{city}%")
        if keywords:
            cond += " AND (title LIKE %s OR content_md LIKE %s)"
            params += [f"%{keywords}%", f"%{keywords}%"]
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM guides WHERE {cond}", params)
            total = int(cur.fetchone()["n"])
            cur.execute(
                f"SELECT {self._COLS} FROM guides WHERE {cond} "
                "ORDER BY published_at DESC, created_at DESC LIMIT %s OFFSET %s",
                [*params, limit, offset],
            )
            rows = cur.fetchall()
        return [self._row_to_record(r) for r in rows], total

    def list_mine(self, user_id: int, *, limit: int = 20, offset: int = 0) -> tuple[list[GuideRecord], int]:
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM guides WHERE user_id = %s", (user_id,))
            total = int(cur.fetchone()["n"])
            cur.execute(
                f"SELECT {self._COLS} FROM guides WHERE user_id = %s "
                "ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (user_id, limit, offset),
            )
            rows = cur.fetchall()
        return [self._row_to_record(r) for r in rows], total

    def find_by_idempotency(self, user_id: int, idempotency_key: str) -> GuideRecord | None:
        """幂等命中（D61）：同 `(user_id, key)` 已存在 → 返回那一篇。

        调用点有**两处，缺一不可**：插入**前**查一次（省掉必然失败的 INSERT）、
        撞唯一键**后**再查一次（并发下两个请求可能同时穿过前一次查）。
        """
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._COLS} FROM guides WHERE user_id = %s AND idempotency_key = %s",
                (user_id, idempotency_key),
            )
            row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def list_by_source_trip(
        self, user_id: int, source_trip_id: str, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[GuideRecord], int]:
        """「我在这条行程下发布过几篇」—— 发布前弹窗的判据（D62）。

        ⚠️ **强制带 user_id**：这个问题的答案只可能是"我的"。别人的攻略
        （哪怕 public）不该进发布判据 —— 否则用户会看到"已发布 3 篇"却
        一篇都不是自己的。查询走 `idx_guides_src (user_id, source_trip_id)`。
        """
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM guides WHERE user_id = %s AND source_trip_id = %s",
                (user_id, source_trip_id),
            )
            total = int(cur.fetchone()["n"])
            cur.execute(
                f"SELECT {self._COLS} FROM guides WHERE user_id = %s AND source_trip_id = %s "
                "ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (user_id, source_trip_id, limit, offset),
            )
            rows = cur.fetchall()
        return [self._row_to_record(r) for r in rows], total

    def update(self, user_id: int, guide_id: str, fields: dict[str, Any]) -> GuideRecord | None:
        """`fields` 的键只允许 title/content_md/destination/cover/poi_ids（路由层已收敛）。"""
        allowed = {"title", "content_md", "destination", "cover", "poi_ids"}
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = %s")
            params.append(json.dumps(value) if key == "poi_ids" else value)
        if not sets:
            return self.get_owned(user_id, guide_id)
        sets.append("updated_at = %s")
        params += [utc_now(), guide_id, user_id]
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE guides SET {', '.join(sets)} WHERE id = %s AND user_id = %s", params)
        return self.get_owned(user_id, guide_id)

    def set_visibility(self, user_id: int, guide_id: str, visibility: str) -> GuideRecord | None:
        """publish → public + published_at=now；unpublish → private + published_at 清空。"""
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            if visibility == "public":
                cur.execute(
                    "UPDATE guides SET visibility='public', published_at=%s, updated_at=%s WHERE id=%s AND user_id=%s",
                    (utc_now(), utc_now(), guide_id, user_id),
                )
            else:
                cur.execute(
                    "UPDATE guides SET visibility='private', published_at=NULL, updated_at=%s WHERE id=%s AND user_id=%s",
                    (utc_now(), guide_id, user_id),
                )
        return self.get_owned(user_id, guide_id)

    def delete(self, user_id: int, guide_id: str) -> bool:
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            n = cur.execute("DELETE FROM guides WHERE id = %s AND user_id = %s", (guide_id, user_id))
        return bool(n)


@dataclass
class CommentRecord:
    comment_id: str
    user_id: int
    target_type: str
    target_id: str
    content: str
    created_at: datetime
    author_name: str = ""
    author_type: str = "user"


class CommentRepo:
    """`comments` 表（M11）。多态目标（guide/poi/comment）一张表。"""

    def __init__(self, conn_factory: ConnectionFactory | None = None) -> None:
        self._conn_factory = conn_factory or connect

    @staticmethod
    def _row_to_record(row: dict[str, Any]) -> CommentRecord:
        return CommentRecord(
            comment_id=row["id"], user_id=int(row["user_id"]),
            target_type=row["target_type"], target_id=row["target_id"],
            content=row["content"], created_at=aware(row["created_at"]) or utc_now(),
            author_name=row.get("author_name") or f"用户{row['user_id']}",
            author_type="user",
        )

    _COLS = "c.id, c.user_id, c.target_type, c.target_id, c.content, c.created_at, u.nickname, u.username"

    def _select_from(self) -> str:
        return (
            "FROM comments c LEFT JOIN users u ON u.id = c.user_id "
        )

    @staticmethod
    def _author_name(row: dict[str, Any]) -> str:
        return row.get("nickname") or row.get("username") or f"用户{row['user_id']}"

    def create(self, user_id: int, target_type: str, target_id: str, content: str) -> CommentRecord:
        now = utc_now()
        cid = uuid.uuid4().hex
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO comments (id, user_id, target_type, target_id, content, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (cid, user_id, target_type, target_id, content, now),
            )
        return CommentRecord(
            comment_id=cid, user_id=user_id, target_type=target_type,
            target_id=target_id, content=content, created_at=now,
            author_name=f"用户{user_id}", author_type="user",
        )

    def list(
        self, target_type: str, target_id: str, *, limit: int = 20, offset: int = 0,
    ) -> tuple[list[CommentRecord], int]:
        """目标下的评论（公开读 —— 评论跟着目标的可见性走，路由层负责挡 private）。"""
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM comments WHERE target_type=%s AND target_id=%s",
                (target_type, target_id),
            )
            total = int(cur.fetchone()["n"])
            cur.execute(
                f"SELECT {self._COLS} {self._select_from()} "
                "WHERE c.target_type=%s AND c.target_id=%s "
                "ORDER BY c.created_at DESC LIMIT %s OFFSET %s",
                (target_type, target_id, limit, offset),
            )
            rows = cur.fetchall()
        items = []
        for r in rows:
            rec = CommentRecord(
                comment_id=r["id"], user_id=int(r["user_id"]), target_type=r["target_type"],
                target_id=r["target_id"], content=r["content"],
                created_at=aware(r["created_at"]) or utc_now(),
                author_name=self._author_name(r), author_type="user",
            )
            items.append(rec)
        return items, total

    def get(self, comment_id: str) -> CommentRecord | None:
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._COLS} {self._select_from()} WHERE c.id = %s", (comment_id,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return CommentRecord(
            comment_id=row["id"], user_id=int(row["user_id"]), target_type=row["target_type"],
            target_id=row["target_id"], content=row["content"],
            created_at=aware(row["created_at"]) or utc_now(),
            author_name=self._author_name(row), author_type="user",
        )

    def delete(self, comment_id: str) -> bool:
        """⚠️ 归属判断在路由层（自己的评论 或 自己攻略下的评论）—— 两条 WHERE 形状不同。"""
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            n = cur.execute("DELETE FROM comments WHERE id = %s", (comment_id,))
        return bool(n)

    def counts(self, target_type: str, target_ids: list[str]) -> dict[str, int]:
        if not target_ids:
            return {}
        placeholders = ", ".join(["%s"] * len(target_ids))
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT target_id, COUNT(*) AS n FROM comments "
                f"WHERE target_type=%s AND target_id IN ({placeholders}) GROUP BY target_id",
                [target_type, *target_ids],
            )
            return {r["target_id"]: int(r["n"]) for r in cur.fetchall()}


class LikeRepo:
    """`likes` 表（M11）。toggle 二合一 + 唯一索引兜底（schemas.LikeToggleRequest 注释）。"""

    def __init__(self, conn_factory: ConnectionFactory | None = None) -> None:
        self._conn_factory = conn_factory or connect

    def toggle(self, user_id: int, target_type: str, target_id: str) -> tuple[bool, int]:
        """返回 (最终 liked 状态, 目标总赞数)。并发靠主键 (user_id,target_type,target_id) 兜底。"""
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            n = cur.execute(
                "SELECT 1 FROM likes WHERE user_id=%s AND target_type=%s AND target_id=%s",
                (user_id, target_type, target_id),
            )
            if n:
                cur.execute(
                    "DELETE FROM likes WHERE user_id=%s AND target_type=%s AND target_id=%s",
                    (user_id, target_type, target_id),
                )
                liked = False
            else:
                try:
                    cur.execute(
                        "INSERT INTO likes (user_id, target_type, target_id, created_at) VALUES (%s, %s, %s, %s)",
                        (user_id, target_type, target_id, utc_now()),
                    )
                    liked = True
                except pymysql.err.IntegrityError:
                    # 并发下另一个请求先插了 —— 等价于"已经点过"，当作取消会歧义，
                    # 这里保守返回 liked=True（用户意图是点赞）
                    liked = True
            cur.execute(
                "SELECT COUNT(*) AS n FROM likes WHERE target_type=%s AND target_id=%s",
                (target_type, target_id),
            )
            count = int(cur.fetchone()["n"])
        return liked, count

    def state(self, user_id: int, target_type: str, target_id: str) -> tuple[bool, int]:
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            n = cur.execute(
                "SELECT 1 FROM likes WHERE user_id=%s AND target_type=%s AND target_id=%s",
                (user_id, target_type, target_id),
            )
            cur.execute(
                "SELECT COUNT(*) AS n FROM likes WHERE target_type=%s AND target_id=%s",
                (target_type, target_id),
            )
            count = int(cur.fetchone()["n"])
        return bool(n), count

    def counts(self, target_type: str, target_ids: list[str]) -> dict[str, int]:
        if not target_ids:
            return {}
        placeholders = ", ".join(["%s"] * len(target_ids))
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT target_id, COUNT(*) AS n FROM likes "
                f"WHERE target_type=%s AND target_id IN ({placeholders}) GROUP BY target_id",
                [target_type, *target_ids],
            )
            return {r["target_id"]: int(r["n"]) for r in cur.fetchall()}


@dataclass
class FavoriteRecord:
    user_id: int
    target_type: str
    target_id: str
    name: str
    cover: str | None
    created_at: datetime


class FavoriteRepo:
    """`favorites` 表（M9 后半）。

    🔴 `name`/`cover` 是**收藏时刻的快照**：收录库（D70）只覆盖**被收录的那部分**
    POI，行程里出现的地点多数不在其中 → 取消收藏后再查不到名字。所以创建时落一份快照，
    `FavoriteItem.name` 才能不靠二次请求拼出来。
    """

    def __init__(self, conn_factory: ConnectionFactory | None = None) -> None:
        self._conn_factory = conn_factory or connect

    @staticmethod
    def _row_to_record(row: dict[str, Any]) -> FavoriteRecord:
        return FavoriteRecord(
            user_id=int(row["user_id"]), target_type=row["target_type"],
            target_id=row["target_id"], name=row["name"], cover=row["cover"],
            created_at=aware(row["created_at"]) or utc_now(),
        )

    def add(
        self, user_id: int, target_type: str, target_id: str, name: str, cover: str | None = None,
    ) -> FavoriteRecord:
        """幂等：重复收藏返回已有那条（不报 409 —— 收藏是个开关，双击不该报错）。"""
        now = utc_now()
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            n = cur.execute(
                "INSERT IGNORE INTO favorites (user_id, target_type, target_id, name, cover, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, target_type, target_id, name, cover, now),
            )
            if not n:  # 已存在 → 回读快照
                cur.execute(
                    "SELECT user_id, target_type, target_id, name, cover, created_at FROM favorites "
                    "WHERE user_id=%s AND target_type=%s AND target_id=%s",
                    (user_id, target_type, target_id),
                )
                return self._row_to_record(cur.fetchone())
        return FavoriteRecord(
            user_id=user_id, target_type=target_type, target_id=target_id,
            name=name, cover=cover, created_at=now,
        )

    def list(
        self, user_id: int, *, target_type: str | None = None, limit: int = 20, offset: int = 0,
    ) -> tuple[list[FavoriteRecord], int]:
        cond, params = "user_id = %s", [user_id]
        if target_type:
            cond += " AND target_type = %s"
            params.append(target_type)
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM favorites WHERE {cond}", params)
            total = int(cur.fetchone()["n"])
            cur.execute(
                f"SELECT user_id, target_type, target_id, name, cover, created_at FROM favorites "
                f"WHERE {cond} ORDER BY created_at DESC LIMIT %s OFFSET %s",
                [*params, limit, offset],
            )
            rows = cur.fetchall()
        return [self._row_to_record(r) for r in rows], total

    def remove(self, user_id: int, target_type: str, target_id: str) -> bool:
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            n = cur.execute(
                "DELETE FROM favorites WHERE user_id=%s AND target_type=%s AND target_id=%s",
                (user_id, target_type, target_id),
            )
        return bool(n)


class SpotRepo:
    """`spots` 收录库（D70）—— **本文件里唯一不带 `user_id` 的 repo**。

    🔴 它是 D31「三重防线」的**明文例外**，理由必须写在这儿，否则下一个人会以为漏写了：
    D31 的判据不是"所有 repo 都带 user_id"，而是「**这条数据被别人看到算不算越权**」。
    收录库是**共享只读内容** —— 所有登录用户浏览同一份景点表，这正是设计本身。
    （对照：`favorites` 也存 name/cover，但那是"某人的收藏"，所以它必须带 user_id。）
    一句话：**带 user_id 是为了防越权，不是为了整齐。**

    为什么要落库：景点页要搜「**收录的**景点」，而收录 = 把**真实高德返回**存成快照。
    手写 5 条 INSERT 是编数据，与 D20 删掉参考设计那句"收录 21006 个景点"是同一个错 ——
    所以**写入路径只有 `scripts/seed_spots.py` 一条**，且它必须走真 provider（real 档）。
    另一条理由是高德 0.45s/次的出站限速（D35）：搜索页翻页/反复搜在本地是零成本。
    """

    def __init__(self, conn_factory: ConnectionFactory | None = None) -> None:
        self._conn_factory = conn_factory or connect

    _COLUMNS = (
        "poi_id, name, city, district, address, lng, lat, "
        "cost_per_person, rating, photos, typecode, open_time"
    )

    @staticmethod
    def _row_to_card(row: dict[str, Any]) -> SpotCard:
        photos = row["photos"]
        if isinstance(photos, str):  # JSON 列经 pymysql 回来是字符串，不自动解析
            photos = json.loads(photos)
        cost = row["cost_per_person"]
        return SpotCard(
            poi_id=row["poi_id"],
            name=row["name"],
            city=row["city"],
            district=row["district"],
            address=row["address"],
            lng=float(row["lng"]),
            lat=float(row["lat"]),
            cost_per_person=float(cost) if cost is not None else None,
            rating=row["rating"],
            photos=list(photos or []),
            typecode=row["typecode"],
        )

    def search(
        self, keywords: str, *, city: str | None = None, limit: int = 20, offset: int = 0
    ) -> tuple[list[SpotCard], int]:
        """按名称 / 别名模糊搜收录库。**排序依据是可解释的两条**：

        1. 名称**前缀**命中优先（`成都博物馆` 搜「成都」排在 `金沙遗址博物馆` 前）
        2. 评分降序（`rating` 是 VARCHAR，用 `CAST(NULLIF(...))` 转数值 —— 空值 NULL 排最后）

        ⚠️ 这不是"热度排序"（D20 明确拒绝自造指标）：`rating` 是高德真实字段，
        缺失时排最后而不是当 0 分 —— 那条纪律和 `AmapPoi` 的 `str | [] → None` 一致。
        """
        kw = keywords.strip()
        like = f"%{kw}%"
        conds = ["(name LIKE %s OR IFNULL(alias, '') LIKE %s)"]
        params: list[Any] = [like, like]
        if city:
            # 用 LIKE 而不是 `=`：高德返回的是「成都市」，前端传的是「成都」
            conds.append("IFNULL(city, '') LIKE %s")
            params.append(f"%{city}%")
        where = " AND ".join(conds)

        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM spots WHERE {where}", params)
            total = int(cur.fetchone()["n"])
            cur.execute(
                f"SELECT {self._COLUMNS} FROM spots WHERE {where} "
                "ORDER BY (name LIKE %s) DESC, "
                "CAST(NULLIF(rating, '') AS DECIMAL(4,2)) DESC, name "
                "LIMIT %s OFFSET %s",
                [*params, f"{kw}%", limit, offset],
            )
            rows = cur.fetchall()
        return [self._row_to_card(r) for r in rows], total

    def get(self, poi_id: str) -> SpotCard | None:
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._COLUMNS} FROM spots WHERE poi_id = %s", (poi_id,)
            )
            row = cur.fetchone()
        return self._row_to_card(row) if row else None

    def upsert_many(
        self, pois: Iterable[AmapPoi], *, source: str = "seed"
    ) -> tuple[int, int]:
        """收录（幂等）：返回 `(新增, 更新)`。**只由 seed 脚本调用。**

        写成显式 SELECT + INSERT/UPDATE 而不是 `ON DUPLICATE KEY UPDATE`：
        后者在 MySQL 8.0.20+ 语法被废弃（`VALUES(col)`），而换成行别名语法
        又会悄悄要求 8.0.19+。两条查询换来"没有版本假设 + 能报准确的增/改条数"，
        收录是低频动作，不差这一次往返。
        """
        inserted = updated = 0
        now = utc_now()
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            for poi in pois:
                row = (
                    poi.poi_id,
                    poi.name,
                    "|".join(poi.alias or []),
                    poi.cityname,
                    poi.adname,
                    poi.address,
                    poi.lng,
                    poi.lat,
                    poi.cost_per_person,
                    poi.rating,
                    json.dumps(list(poi.photos or []), ensure_ascii=False),
                    poi.typecode,
                    poi.type,
                    poi.open_time,
                    poi.adcode,
                )
                cur.execute("SELECT poi_id FROM spots WHERE poi_id = %s", (poi.poi_id,))
                exists = cur.fetchone() is not None
                if exists:
                    cur.execute(
                        "UPDATE spots SET name=%s, alias=%s, city=%s, district=%s, address=%s, "
                        "lng=%s, lat=%s, cost_per_person=%s, rating=%s, photos=%s, typecode=%s, "
                        "type=%s, open_time=%s, adcode=%s, source=%s, updated_at=%s "
                        "WHERE poi_id=%s",
                        (*row[1:], source, now, poi.poi_id),
                    )
                    updated += 1
                else:
                    cur.execute(
                        "INSERT INTO spots (poi_id, name, alias, city, district, address, lng, lat, "
                        "cost_per_person, rating, photos, typecode, type, open_time, adcode, "
                        "source, collected_at, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                        "%s, %s, %s)",
                        (*row, source, now, now),
                    )
                    inserted += 1
        return inserted, updated

    def count(self) -> int:
        with closing(self._conn_factory()) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM spots")
            return int(cur.fetchone()["n"])


__all__ = [
    "SessionRepo",
    "TripRepo",
    "UserRepo",
    "UserRecord",
    "GuideRepo",
    "GuideRecord",
    "CommentRepo",
    "CommentRecord",
    "LikeRepo",
    "FavoriteRepo",
    "FavoriteRecord",
    "SpotRepo",
    "DEFAULT_SESSION_TITLE",
]
