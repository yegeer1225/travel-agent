"""限流（D28 的落地）：**第一目的不是防攻击，是防钱**。

═══════════════════════════════════════════════════════════════
 实现：进程内滑动窗口，不引 Redis
═══════════════════════════════════════════════════════════════

每个 `(规则名, 维度标识)` 一个 deque，存最近 N 秒内的命中时间戳；
检查时先把窗口外的弹掉，再看窗口内还有没有余额。
**滑动**（不是固定窗口）的意义：固定窗口在边界处能瞬间放过 2 倍配额
（23:59:59 十次 + 00:00:01 十次），而 chat 的配额就是真金白银。

D28 明确接受的两个代价：进程重启计数清零（软限流可接受）；
多实例部署时各实例独立计数会超发 —— **重审触发就是"多实例部署"**，
单 worker 部署下这套是精确的。

⚠️ 时间源用 `time.monotonic` 而不是 `time.time`：前者不受系统校时/回拨影响，
窗口计算不会因为 NTP 校准出现负数或漏判。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from fastapi import Depends, Request

from app.api.deps import get_current_user_id
from app.api.errors import RateLimited


@dataclass(frozen=True)
class Rule:
    """一条限流规则（阈值全部来自 D28 的表，不要在这里现场发明数字）。"""

    name: str
    max_requests: int
    window_seconds: int
    scope: str
    """`user`（按用户）｜`ip`（按来源 IP，攻击者还没有账号时用）｜`session`（按会话）。"""


class SlidingWindowLimiter:
    """滑动窗口计数器。**与 FastAPI 无关**，单独一个类 = 单测不用起 HTTP 栈。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._clock = clock

    def check(self, key: str, rule: Rule) -> int:
        """判一次。返回 0 = 放行（这次已计入）；返回正数 = 拒绝，值为建议等待秒数。"""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        cutoff = now - rule.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= rule.max_requests:
            # 第 (N+1) 次要等"最早那次滚出窗口"才能进来 —— 保守向上取整 1 秒
            retry_after = int(hits[0] + rule.window_seconds - now) + 1
            return max(1, retry_after)

        hits.append(now)
        return 0

    def reset(self) -> None:
        """清空（测试用；生产里进程重启天然清零，D28 已接受）。"""
        self._hits.clear()


# ══════════════════════════════════════════════════════════════
#  D28 阈值表 —— 阈值只写在这里，路由里引用名字
# ══════════════════════════════════════════════════════════════

CHAT_HOURLY = Rule("chat_hourly", 10, 3600, "user")
"""`POST /sessions/{id}/chat`：10 / 小时。"""
CHAT_DAILY = Rule("chat_daily", 30, 86400, "user")
"""`POST /sessions/{id}/chat`：30 / 天。"""
PASTE_HOURLY = Rule("paste_hourly", 20, 3600, "user")
"""`POST /trips/paste`：20 / 小时。"""
RECHECK_HOURLY = Rule("recheck_hourly", 60, 3600, "user")
"""`POST /trips/{id}/recheck`：60 / 小时。"""
SPOT_SEARCH_PER_MIN = Rule("spot_search", 120, 60, "user")
"""`GET /spots/search`：120 / 分钟。"""
AUTH_PER_MIN = Rule("auth_ip", 10, 60, "ip")
"""`/auth/register`·`login`：10 / 分钟，按 **IP**（攻击者还没有账号）。"""
AVATAR_HOURLY = Rule("avatar_hourly", 5, 3600, "user")
"""`POST /users/me/avatar`：5 / 小时。"""
GLOBAL_IP = Rule("global_ip", 300, 60, "ip")
"""全局兜底：300 / 分钟 · IP。挂在**整个 API 路由**上，防单机脚本。"""


#: 同一 session 并发流 = 同时 1 个。**它不是次数窗口**，是"当前有没有人在跑"：
#: D28 说"这条比次数限流重要"。实现放 M6 chat 路由里（用一个 set[session_id]
#: 进出配对），这里只留注释占位 —— 在没有 chat 的 M5 写它就是死代码。


def make_dependency(limiter: SlidingWindowLimiter, rule: Rule) -> Callable:
    """造一个 FastAPI 依赖。key = `规则名:scope:标识` —— 不同规则天然不串。"""

    async def _dep(
        request: Request,
        # 🔴 这里必须 `= Depends(...)`：裸参数会被 FastAPI 当成"必填 query 参数"，
        # 于是每个被限流的路由都多出一个看不见的 user_id 查询参数，
        # 前端不带就 400 —— 实测冒烟时抓到的。
        user_id: int = Depends(get_current_user_id),
    ) -> None:
        if rule.scope == "user":
            identifier = str(user_id)
        elif rule.scope == "ip":
            # request.client 在测试客户端下可能是 None（TestClient 用 "testclient"）
            identifier = (request.client.host if request.client else "unknown")
        else:  # pragma: no cover - session 级规则 M6 接 chat 时才会用到
            identifier = str(request.path_params.get("session_id", "unknown"))
        retry_after = limiter.check(f"{rule.name}:{identifier}", rule)
        if retry_after:
            raise RateLimited(f"操作太频繁，请 {retry_after} 秒后重试", retry_after)

    return _dep


__all__ = [
    "Rule",
    "SlidingWindowLimiter",
    "make_dependency",
    "CHAT_HOURLY",
    "CHAT_DAILY",
    "PASTE_HOURLY",
    "RECHECK_HOURLY",
    "SPOT_SEARCH_PER_MIN",
    "AUTH_PER_MIN",
    "AVATAR_HOURLY",
    "GLOBAL_IP",
]
