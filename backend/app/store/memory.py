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
from app.store.repo import DEFAULT_SESSION_TITLE, UserRecord


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


__all__ = ["InMemorySessionStore", "InMemoryTripStore"]
