"""跨模块的小工具函数。

为什么单独一个文件：`_text_of` 原来住在 `graph/nodes.py`，M4 时代的
`subagent.py` 也要用它，而 `nodes.py` 同时要 import `subagent`
的 trace 通道 —— 两边互相 import 就是循环依赖（Python 会静默给你一个
半初始化的模块，报错位置离病根十万八千里）。subagent.py 已退役（P1/D79），
但"被两个互有依赖的模块共用的东西，挪出去"这条判断标准留着。
"""

from __future__ import annotations

import math
from typing import Any

EARTH_RADIUS_KM = 6371.0088
"""地球平均半径。取这个值是为了和 `providers/amap.py` 原来的本地实现逐位一致 ——
换值会让所有距离断言（含 `calc_distance` 的测试）跟着动，收益却为零。"""


def text_of(message: Any) -> str:
    """把模型返回的 `content` 取成字符串。

    ⚠️ `content` 不一定是 `str` —— 多模态返回是 `list[dict]`。
    直接 `.strip()` 会在那种情况下抛 `AttributeError`，
    而它只在"模型返回了非文本内容"时才出现，很难复现。
    """
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "".join(parts)
    return str(content)


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """球面直线距离，单位 km。**参数是 `(lng, lat)`** —— 高德惯例，别和 GeoJSON 搞反。

    原来 `providers/amap.py` 与 `providers/mock.py` 各有一份（算法逐行相同），
    搜索结果按中心点加权（D75）又要用第三份 → 挪到这里共用。
    两份都写过的坑：**经度在前**。写反了算出来的距离物理上说不通，
    但代码不会报错，只会静静地把排序搞乱。
    """
    lng1, lat1 = a
    lng2, lat2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


__all__ = ["EARTH_RADIUS_KM", "haversine_km", "text_of"]
