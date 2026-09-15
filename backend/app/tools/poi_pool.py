"""封闭世界校验的 POI 池 —— **本项目可信度的核心机制**。

一句话说清它解决什么：

> 模型会编 POI。唯一能证明"这个景点不是我编的"的办法，是
> **它必须能在某个工具真的返回过的候选池里找到**。

所以池子的定位不是缓存，是**判据的来源**：模型出的每个 `poi_id` 都要回来对一遍，
对不上就是硬错（`poi_exists` 判 `fail`）。

═══════════════════════════════════════════════════════════════
 为什么用 `contextvars` 而不是实例属性 / 全局变量
═══════════════════════════════════════════════════════════════

| 方案 | 为什么不行 |
|---|---|
| 挂在 provider 上 | provider 是**无状态单例**，多个请求共用一个，池子会串台 |
| 模块级全局变量 | 同上，而且 `asyncio` 下更难查 |
| 挂在图 state 上 | **工具拿不到 state** —— 工具只返回给模型看的文本，图 state 由节点持有 |
| ✅ `contextvars` | 每个 `asyncio` 任务（= 每个请求）一份，**天然隔离、不需要锁** |

最后一行是关键：LangGraph 的工具函数签名里拿不到图 state，
所以"工具往里写、校验层往外读"必须经由一个**比请求生命周期略长、又不跨请求**的容器。
`contextvars` 正好就是这个粒度。

═══════════════════════════════════════════════════════════════
 用法
═══════════════════════════════════════════════════════════════

```python
with poi_pool_scope() as pool:      # 每个请求 / 每次命令行运行包一层
    await run_graph(...)            # 工具内部自动 record()
    pool.all()                      # 校验层读全量
```
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from app.schemas import AmapPoi


class PoiPool:
    """一次运行中被工具真实返回过的 POI 全集。

    **只增不改**：同一个 `poi_id` 重复出现时保留第一次的版本。
    理由是"池子里的值应当反映第一次见到它时的样子" ——
    如果后来的覆盖了先前的，校验时看到的数据和模型当时看到的不一致，
    出了分歧将无法复盘。
    """

    __slots__ = ("_by_id", "_order")

    def __init__(self) -> None:
        self._by_id: dict[str, AmapPoi] = {}
        self._order: list[str] = []

    def record(self, pois: list[AmapPoi]) -> int:
        """记下这次工具返回的 POI。返回**新增**条数（重复的不算）。"""
        added = 0
        for poi in pois:
            if poi.poi_id in self._by_id:
                continue
            self._by_id[poi.poi_id] = poi
            self._order.append(poi.poi_id)
            added += 1
        return added

    def get(self, poi_id: str) -> AmapPoi | None:
        """按 id 取。**取不到 = 模型编的**（或它记错了 id）。"""
        return self._by_id.get(poi_id)

    def contains(self, poi_id: str) -> bool:
        return poi_id in self._by_id

    def all(self) -> list[AmapPoi]:
        """按**首次出现顺序**返回 —— 这个顺序比字典插入序更稳定，
        且日志里"模型先搜到什么"是有用信息。"""
        return [self._by_id[i] for i in self._order]

    def ids(self) -> list[str]:
        return list(self._order)

    def find_by_name(self, name: str) -> AmapPoi | None:
        """按名字或别名精确/包含匹配。

        ⚠️ 这是**给校验层兜底**用的（风险 3）：模型写「武侯祠」而池子里是
        「成都武侯祠博物馆」时，靠别名把它兜住。
        **但它不能用来放宽 `poi_id` 校验** —— id 对不上就是硬错，
        名字像不像只影响"要不要给出更友好的提示"。
        """
        if not name:
            return None
        target = name.strip()
        for poi in self.all():
            if poi.name == target or target in poi.alias:
                return poi
        for poi in self.all():
            if target in poi.name or poi.name in target:
                return poi
        return None

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, poi_id: object) -> bool:
        return isinstance(poi_id, str) and poi_id in self._by_id

    def describe(self) -> str:
        return f"PoiPool({len(self)} 个 POI)"


_active: ContextVar[PoiPool | None] = ContextVar("travel_agent_poi_pool", default=None)


def current_pool() -> PoiPool | None:
    """当前上下文里的池子。**没有池子时返回 `None` 而不是造一个** ——
    悄悄造一个会让"忘了开 scope"变成静默失效（池子永远是空的，
    所有 POI 都被判成"模型编的"），那种 bug 极难查。"""
    return _active.get()


def record(pois: list[AmapPoi]) -> int:
    """工具调用这个。没有活动池子时**静默返回 0**。

    为什么静默而不是报错：工具可能被单独调用做调试（比如直接 `await search_poi(...)`）。
    但**校验层不允许依赖这个静默行为** —— 它必须能区分
    "池子是空的（忘开 scope）"和"模型一个 POI 都没引用"。
    区分方式是 `current_pool() is None`（见 `describe` 的用法）。
    """
    pool = _active.get()
    if pool is None:
        return 0
    return pool.record(pois)


@contextmanager
def poi_pool_scope(pool: PoiPool | None = None) -> Iterator[PoiPool]:
    """开一个池子作用域。

    嵌套会**各是各的**（`ContextVar` 语义），不会共享 —— 这是对的：
    子图不该看到父图的池子，否则"子 agent 上下文隔离"（M4）就是假的。
    """
    active = pool or PoiPool()
    token: Token = _active.set(active)
    try:
        yield active
    finally:
        _active.reset(token)


__all__ = [
    "PoiPool",
    "current_pool",
    "poi_pool_scope",
    "record",
]
