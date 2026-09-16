"""限流器单测（D28 落地）。**不起 HTTP 栈** —— `SlidingWindowLimiter` 是纯类。

时间用假时钟（可控前进），这是"滑动窗口行为"唯一可靠的测法：
真 `time.monotonic` 下你没法让窗口"刚刚滑过去"。
"""

from __future__ import annotations

from app.api.ratelimit import GLOBAL_IP, Rule, SlidingWindowLimiter


class FakeClock:
    """可控时钟。`advance()` 模拟时间流逝 —— 滑动窗口测试的核心工具。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_allows_up_to_max_then_rejects():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(clock=clock)
    rule = Rule("r", max_requests=3, window_seconds=60, scope="user")

    assert limiter.check("k", rule) == 0
    assert limiter.check("k", rule) == 0
    assert limiter.check("k", rule) == 0
    assert limiter.check("k", rule) > 0, "第 4 次必须被拒"


def test_retry_after_points_to_when_the_window_frees_up():
    """retry_after ≈ 最早一次命中滚出窗口的时间 —— 不是拍脑袋的固定值。"""
    clock = FakeClock()
    limiter = SlidingWindowLimiter(clock=clock)
    rule = Rule("r", max_requests=1, window_seconds=60, scope="user")

    limiter.check("k", rule)  # t=1000
    clock.advance(10)  # t=1010
    retry_after = limiter.check("k", rule)

    # 最早命中在 1000，窗口 60 → 1060 才空出来；现在 1010 → 等 ~50 秒
    assert 49 <= retry_after <= 51, f"retry_after={retry_after}"


def test_window_slides():
    """窗口外的老命中被弹出 —— 等 61 秒后配额完全恢复。"""
    clock = FakeClock()
    limiter = SlidingWindowLimiter(clock=clock)
    rule = Rule("r", max_requests=2, window_seconds=60, scope="user")

    limiter.check("k", rule)
    limiter.check("k", rule)
    assert limiter.check("k", rule) > 0

    clock.advance(61)
    assert limiter.check("k", rule) == 0, "窗口滑走后必须恢复"


def test_keys_are_isolated():
    """不同 key 各自计数 —— 用户 A 打满不影响用户 B。"""
    limiter = SlidingWindowLimiter()
    rule = Rule("r", max_requests=1, window_seconds=60, scope="user")

    limiter.check("alice", rule)
    assert limiter.check("bob", rule) == 0, "bob 的第一次不该被 alice 的记录挡住"


def test_sliding_not_fixed_window():
    """🔴 这条是"滑动"二字的存在证明：固定窗口在边界处能放过 2 倍配额。

    窗口 60 秒、限额 2：t=0 用 1 次，t=59.9 用 1 次。
    固定窗口法在 t=60.1 进入"新窗口"（计数清零），t=60.1、t=60.2 都能过
    → 60.0~60.2 这 0.2 秒里实际放过了 3 次请求。
    滑动窗口看到 [0.2, 60.2] 里已有 59.9 / 60.1 两次 → 第 3 次拒绝。
    """
    clock = FakeClock()
    limiter = SlidingWindowLimiter(clock=clock)
    rule = Rule("r", max_requests=2, window_seconds=60, scope="user")

    limiter.check("k", rule)  # t=1000
    clock.advance(59.9)
    limiter.check("k", rule)  # t=1059.9，窗口 [999.9, 1059.9] 内 2 次
    clock.advance(0.2)  # t=1060.1：t=1000 的老命中刚滑出窗口 → 允许 1 次
    assert limiter.check("k", rule) == 0
    clock.advance(0.1)  # t=1060.2：窗口 [1000.2, 1060.2] 内已有 1059.9 / 1060.1
    assert limiter.check("k", rule) > 0, "0.2 秒内第 3 次必须被拒（固定窗口法这里会放过）"


def test_d28_global_rule_values_are_pinned():
    """🔴 阈值是 D28 拍板的数字，被人"顺手调宽"就是改决策 —— 用断言钉住。"""
    assert GLOBAL_IP.max_requests == 300
    assert GLOBAL_IP.window_seconds == 60
    assert GLOBAL_IP.scope == "ip"
