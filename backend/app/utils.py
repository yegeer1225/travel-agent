"""跨模块的小工具函数。

为什么单独一个文件：`_text_of` 原来住在 `graph/nodes.py`，
M4 的 `subagent.py` 也要用它，而 `nodes.py` 同时要 import `subagent`
的 trace 通道 —— 两边互相 import 就是循环依赖（Python 会静默给你一个
半初始化的模块，报错位置离病根十万八千里）。
判断标准就一条：**被两个互有依赖的模块共用的东西，挪出去**。
"""

from __future__ import annotations

from typing import Any


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


__all__ = ["text_of"]
