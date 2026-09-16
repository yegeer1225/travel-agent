"""repo 层的测试替身/包装 —— 只在测试里用，所以放 tests/ 而不是 app/。"""

from __future__ import annotations

from typing import Any


class BoomTripStore:
    """包一层"一查就炸"的 trip store —— 构造 500 场景用。"""

    def __init__(self, inner) -> None:
        self._inner = inner

    def save(self, user_id: int, trip) -> None:  # noqa: ANN001
        self._inner.save(user_id, trip)

    def list(self, user_id: int, *, limit: int = 20, offset: int = 0):  # noqa: ANN001, ANN201
        return self._inner.list(user_id, limit=limit, offset=offset)

    def get(self, user_id: int, trip_id: str) -> Any:
        raise RuntimeError("数据库炸了（测试专用）")
