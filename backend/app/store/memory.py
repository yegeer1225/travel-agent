"""InMemory 版 repo —— **与 MySQL 版同签名**，测试与无库环境用。

和 `providers/mock.py` 同一个设计理由：接口层的测试不该连真库
（`requirements.txt` 里写死的纪律：tests 不连数据库）。
同签名意味着路由层代码在"测试替身"和"MySQL 实现"之间**零改动**切换 ——
切换本身只发生在 `deps.py` 的组装点，那一处是唯一需要测真库的地方
（放 `scripts/verify_mysql_api.py`，Docker 起来后跑）。

⚠️ 用真实 Pydantic 模型存取（不是 dict），这样"repo 返回契约模型"
这个约定在测试里和真库版行为一致。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.schemas import ChatMessage, MessageMeta, MessageRole, Session, Trip, TripSummaryItem
from app.store.repo import DEFAULT_SESSION_TITLE, DuplicateIdempotencyKeyError, UserRecord


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryUserStore:
    """进程内字典版 users（M9）。接口与 UserRepo 逐一对应，测试用。"""

    def __init__(self) -> None:
        self._rows: dict[int, dict] = {}
        self._next_id = 1

    def create(
        self,
        username: str,
        password_hash: str,
        *,
        nickname: str | None = None,
    ) -> UserRecord:
        uid = self._next_id
        self._next_id += 1
        now = _now()
        self._rows[uid] = {
            "id": uid, "username": username, "password_hash": password_hash,
            "nickname": nickname, "email": None, "avatar": None, "created_at": now,
        }
        return self.get_by_id(uid)  # type: ignore[return-value]

    def get_by_username(self, username: str) -> UserRecord | None:
        for r in self._rows.values():
            if r["username"] == username:
                return UserRecord(
                    id=r["id"], username=r["username"], password_hash=r["password_hash"],
                    nickname=r["nickname"], email=r["email"], avatar=r["avatar"],
                    created_at=r["created_at"],
                )
        return None

    def get_by_id(self, user_id: int) -> UserRecord | None:
        r = self._rows.get(user_id)
        if r is None:
            return None
        return UserRecord(
            id=r["id"], username=r["username"], password_hash=r["password_hash"],
            nickname=r["nickname"], email=r["email"], avatar=r["avatar"],
            created_at=r["created_at"],
        )

    def update_profile(
        self,
        user_id: int,
        *,
        nickname: str | None = None,
        email: str | None = None,
        avatar: str | None = None,
    ) -> UserRecord | None:
        r = self._rows.get(user_id)
        if r is None:
            return None
        if nickname is not None:
            r["nickname"] = nickname
        if email is not None:
            r["email"] = email
        if avatar is not None:
            r["avatar"] = avatar
        return self.get_by_id(user_id)


class InMemorySessionStore:
    """进程内字典版 sessions。**顺序语义与 SQL 版对齐**：列表按 updated_at 倒序。"""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def create(
        self,
        user_id: int,
        title: str | None = None,
        *,
        session_id: str | None = None,
        model: str | None = None,
    ) -> Session:
        now = _now()
        sid = session_id or uuid.uuid4().hex
        self._rows[sid] = {"user_id": user_id, "title": title or DEFAULT_SESSION_TITLE, "model": model, "created_at": now, "updated_at": now}
        return Session(session_id=sid, title=title or DEFAULT_SESSION_TITLE, model=model, created_at=now, updated_at=now)

    def list(self, user_id: int, *, limit: int = 20, offset: int = 0) -> tuple[list[Session], int]:
        pairs = [(sid, r) for sid, r in self._rows.items() if r["user_id"] == user_id]
        pairs.sort(key=lambda p: p[1]["updated_at"], reverse=True)
        items = [
            Session(
                session_id=sid, title=r["title"], model=r.get("model"),
                created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for sid, r in pairs[offset : offset + limit]
        ]
        return items, len(pairs)

    def get(self, user_id: int, session_id: str) -> Session | None:
        row = self._rows.get(session_id)
        if row is None or row["user_id"] != user_id:
            return None
        return Session(
            session_id=session_id, title=row["title"], model=row.get("model"),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def delete(self, user_id: int, session_id: str) -> bool:
        row = self._rows.get(session_id)
        if row is None or row["user_id"] != user_id:
            return False
        del self._rows[session_id]
        return True

    # ══════════ M6：消息（与 MySQL 版同签名） ══════════

    def append_message(
        self,
        user_id: int,
        session_id: str,
        *,
        role: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> ChatMessage:
        row = self._rows.get(session_id)
        if row is None or row["user_id"] != user_id:
            raise KeyError(session_id)
        now = _now()
        msg = ChatMessage(
            id=uuid.uuid4().hex, role=MessageRole(role), content=content,
            meta=MessageMeta.model_validate(meta) if meta else None, created_at=now,
        )
        row.setdefault("messages", []).append(msg)
        row["updated_at"] = now
        return msg

    def list_messages(self, user_id: int, session_id: str) -> list[ChatMessage]:
        row = self._rows.get(session_id)
        if row is None or row["user_id"] != user_id:
            raise KeyError(session_id)
        return list(row.get("messages", []))

    def update_title(self, user_id: int, session_id: str, title: str) -> None:
        row = self._rows.get(session_id)
        if row is None or row["user_id"] != user_id:
            raise KeyError(session_id)
        row["title"] = title[:100]
        row["updated_at"] = _now()


class InMemoryTripStore:
    """进程内字典版 trips。存完整 `Trip` 模型，列表只投影出卡面字段。"""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def save(self, user_id: int, trip: Trip) -> None:
        now = _now()
        existing = self._rows.get(trip.trip_id)
        self._rows[trip.trip_id] = {
            "user_id": user_id,
            "trip": trip,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }

    def list(self, user_id: int, *, limit: int = 20, offset: int = 0) -> tuple[list[TripSummaryItem], int]:
        rows = [r for r in self._rows.values() if r["user_id"] == user_id]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        items = []
        for r in rows[offset : offset + limit]:
            trip: Trip = r["trip"]
            items.append(
                TripSummaryItem(
                    trip_id=trip.trip_id, session_id=trip.session_id, title=trip.title,
                    destination=trip.destination, source=trip.source,
                    created_at=r["created_at"], updated_at=r["updated_at"], summary=trip.summary,
                )
            )
        return items, len(rows)

    def get(self, user_id: int, trip_id: str) -> Trip | None:
        row = self._rows.get(trip_id)
        if row is None or row["user_id"] != user_id:
            return None
        return row["trip"]


__all__ = [
    "InMemorySessionStore",
    "InMemoryTripStore",
    "InMemoryGuideStore",
    "InMemoryCommentStore",
    "InMemoryLikeStore",
    "InMemoryFavoriteStore",
]


# ══════════════════════════════════════════════════════════════
#  M10/M11 内存替身 —— 与 GuideRepo / CommentRepo / LikeRepo / FavoriteRepo 同签名
# ══════════════════════════════════════════════════════════════


class InMemoryGuideStore:
    """进程内字典版 guides。**与 GuideRepo 方法逐一对应**，语义与 SQL 版一致。"""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

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
    ) -> dict[str, Any]:
        if idempotency_key is not None and any(
            r["user_id"] == user_id and r["idempotency_key"] == idempotency_key
            for r in self._rows.values()
        ):
            # 与真库 uq_guides_idem 同语义：同 (user_id, key) 不许有两条
            raise DuplicateIdempotencyKeyError("uq_guides_idem (in-memory)")
        now = _now()
        gid = uuid.uuid4().hex
        rec = {
            "id": gid, "user_id": user_id, "title": title, "content_md": content_md,
            "destination": destination, "cover": cover, "poi_ids": list(poi_ids or []),
            "visibility": "public" if publish else "private",
            "published_at": now if publish else None,
            "source_trip_id": source_trip_id, "idempotency_key": idempotency_key,
            "created_at": now, "updated_at": now,
        }
        self._rows[gid] = rec
        return dict(rec)

    @staticmethod
    def _copy(r: dict[str, Any]) -> dict[str, Any]:
        r = dict(r)
        r["poi_ids"] = list(r["poi_ids"])
        return r

    def get(self, guide_id: str) -> dict[str, Any] | None:
        r = self._rows.get(guide_id)
        return self._copy(r) if r else None

    def get_owned(self, user_id: int, guide_id: str) -> dict[str, Any] | None:
        r = self._rows.get(guide_id)
        if r is None or r["user_id"] != user_id:
            return None
        return self._copy(r)

    def list_public(
        self,
        *,
        city: str | None = None,
        keywords: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [r for r in self._rows.values() if r["visibility"] == "public"]
        if city:
            rows = [r for r in rows if r["destination"] and city in r["destination"]]
        if keywords:
            rows = [r for r in rows if keywords in r["title"] or keywords in r["content_md"]]
        rows.sort(key=lambda r: (r["published_at"] or r["created_at"]), reverse=True)
        return [self._copy(r) for r in rows[offset : offset + limit]], len(rows)

    def list_mine(self, user_id: int, *, limit: int = 20, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        rows = [r for r in self._rows.values() if r["user_id"] == user_id]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [self._copy(r) for r in rows[offset : offset + limit]], len(rows)

    def find_by_idempotency(self, user_id: int, idempotency_key: str) -> dict[str, Any] | None:
        """幂等命中（D61）—— 与 GuideRepo.find_by_idempotency 同签名。"""
        for r in self._rows.values():
            if r["user_id"] == user_id and r["idempotency_key"] == idempotency_key:
                return self._copy(r)
        return None

    def list_by_source_trip(
        self, user_id: int, source_trip_id: str, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """「我在这条行程下发布过几篇」（D62 弹窗判据）—— 与 GuideRepo 同签名。"""
        rows = [
            r for r in self._rows.values()
            if r["user_id"] == user_id and r["source_trip_id"] == source_trip_id
        ]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [self._copy(r) for r in rows[offset : offset + limit]], len(rows)

    _ALLOWED = {"title", "content_md", "destination", "cover", "poi_ids"}

    def update(self, user_id: int, guide_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        r = self._rows.get(guide_id)
        if r is None or r["user_id"] != user_id:
            return None
        for key, value in fields.items():
            if key in self._ALLOWED:
                r[key] = list(value) if key == "poi_ids" else value
        r["updated_at"] = _now()
        return self._copy(r)

    def set_visibility(self, user_id: int, guide_id: str, visibility: str) -> dict[str, Any] | None:
        r = self._rows.get(guide_id)
        if r is None or r["user_id"] != user_id:
            return None
        r["visibility"] = visibility
        r["published_at"] = _now() if visibility == "public" else None
        r["updated_at"] = _now()
        return self._copy(r)

    def delete(self, user_id: int, guide_id: str) -> bool:
        r = self._rows.get(guide_id)
        if r is None or r["user_id"] != user_id:
            return False
        del self._rows[guide_id]
        return True


class InMemoryCommentStore:
    """进程内字典版 comments。作者名从注入的 {user_id: name} 反查。"""

    def __init__(self, users: dict[int, str] | None = None) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._user_names: dict[int, str] = users or {}

    def register_user_name(self, user_id: int, name: str) -> None:
        self._user_names[user_id] = name

    def _author(self, user_id: int) -> str:
        return self._user_names.get(user_id, f"用户{user_id}")

    def create(self, user_id: int, target_type: str, target_id: str, content: str) -> dict[str, Any]:
        cid = uuid.uuid4().hex
        rec = {
            "id": cid, "user_id": user_id, "target_type": target_type,
            "target_id": target_id, "content": content, "created_at": _now(),
        }
        self._rows[cid] = rec
        return {**rec, "author_name": self._author(user_id), "author_type": "user"}

    def list(
        self, target_type: str, target_id: str, *, limit: int = 20, offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [r for r in self._rows.values() if r["target_type"] == target_type and r["target_id"] == target_id]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [
            {**r, "author_name": self._author(r["user_id"]), "author_type": "user"}
            for r in rows[offset : offset + limit]
        ], len(rows)

    def get(self, comment_id: str) -> dict[str, Any] | None:
        r = self._rows.get(comment_id)
        if r is None:
            return None
        return {**r, "author_name": self._author(r["user_id"]), "author_type": "user"}

    def delete(self, comment_id: str) -> bool:
        return self._rows.pop(comment_id, None) is not None

    def counts(self, target_type: str, target_ids: list[str]) -> dict[str, int]:
        return {
            tid: sum(
                1 for r in self._rows.values()
                if r["target_type"] == target_type and r["target_id"] == tid
            )
            for tid in target_ids
        }


class InMemoryLikeStore:
    """进程内字典版 likes。key = (user_id, target_type, target_id)。"""

    def __init__(self) -> None:
        self._rows: set[tuple[int, str, str]] = set()

    def toggle(self, user_id: int, target_type: str, target_id: str) -> tuple[bool, int]:
        key = (user_id, target_type, target_id)
        if key in self._rows:
            self._rows.discard(key)
            liked = False
        else:
            self._rows.add(key)
            liked = True
        return liked, self._count(target_type, target_id)

    def state(self, user_id: int, target_type: str, target_id: str) -> tuple[bool, int]:
        return (user_id, target_type, target_id) in self._rows, self._count(target_type, target_id)

    def _count(self, target_type: str, target_id: str) -> int:
        return sum(1 for _, t, i in self._rows if t == target_type and i == target_id)

    def counts(self, target_type: str, target_ids: list[str]) -> dict[str, int]:
        return {tid: self._count(target_type, tid) for tid in target_ids}


class InMemoryFavoriteStore:
    """进程内字典版 favorites。key = (user_id, target_type, target_id)。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[int, str, str], dict[str, Any]] = {}

    def add(
        self, user_id: int, target_type: str, target_id: str, name: str, cover: str | None = None,
    ) -> dict[str, Any]:
        key = (user_id, target_type, target_id)
        if key in self._rows:
            return dict(self._rows[key])
        rec = {
            "user_id": user_id, "target_type": target_type, "target_id": target_id,
            "name": name, "cover": cover, "created_at": _now(),
        }
        self._rows[key] = rec
        return dict(rec)

    def list(
        self, user_id: int, *, target_type: str | None = None, limit: int = 20, offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [r for r in self._rows.values() if r["user_id"] == user_id]
        if target_type:
            rows = [r for r in rows if r["target_type"] == target_type]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in rows[offset : offset + limit]], len(rows)

    def remove(self, user_id: int, target_type: str, target_id: str) -> bool:
        return self._rows.pop((user_id, target_type, target_id), None) is not None
