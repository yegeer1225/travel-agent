"""数据源抽象层。

**mock 与 real 必须同签名**（D23）。这里用 `Protocol` 而不是 ABC，两个理由：

1. 只约束「有哪些方法」，不要求继承 —— real 实现可能来自第三方包或将来抽成独立服务
2. `Protocol` 是**结构化**检查：`MockAmapProvider` 不用写 `class X(AmapProvider)`，
   静态检查器照样能发现它掉队。少一处要记得改的地方，就少一处会忘的地方。

⚠️ `calc_distance` 在真实世界里是**两次 API 调用之间的一步**，不是独立数据源。
但它是唯一需要联网计算的东西，放进 provider 才能整体被 mock 替换。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from app.schemas import AmapPoi, Weather


@dataclass(frozen=True)
class DistanceResult:
    """两点之间的驾驶距离与耗时。

    ⚠️ `km` 是**驾车里程**不是直线距离。校验层判"当天车程是否过长"用的是这个值，
    所以 provider 有义务给出路网距离 —— mock 用直线 × 系数模拟，
    **这是 mock 与 real 之间唯一一处"量级对但数值不准"的地方**，M2 换真接口后消失。
    """

    km: float
    drive_min: int
    straight_km: float
    """直线距离。保留它是为了在 mock 与 real 之间做交叉验证：
    真驾车距离 / 直线距离 的比值若异常（比如 < 1），说明坐标系或参数搞错了。"""


@runtime_checkable
class AmapProvider(Protocol):
    """高德数据源的统一接口。三个方法 = 三个工具（A7）。"""

    name: str
    """`"mock"` 或 `"real"`。会写进日志和 `GET /health`。"""

    def covers(self, city: str | None) -> bool:
        """**这个数据源能不能覆盖这座城市**（D67-B）。

        用途只有一个：目的地不被覆盖时**别进工具循环** —— 否则会白等。
        实测（2026-09-17）：非成都目的地 → 子代理反复空搜 ≈100s → 撞轮数上限才报错，
        **总计 159s**，全花在搜一个已知搜不到的地方。

        🔴 **必须保守**，因为判错的代价不对称：
        · 判「覆盖不了」其实能 → **拦住一份本可排出的行程**（比白等更糟）
        · 判「能覆盖」其实不能 → 退回白等，但结果仍正确（只是慢）

        所以拿不准就返回 `True`。`AmapHttpProvider` **恒 `True`**（真实数据不分城市）；
        `MockAmapProvider` 按池子的实际覆盖回答 —— 这样切到 `real` 后本判据**自动失效**，
        调用点一行不用改（D23 那层抽象的延续）。
        """
        ...

    async def search_poi(
        self,
        keyword: str,
        city: str | None = None,
        limit: int = 10,
    ) -> list[AmapPoi]:
        """按关键词搜地点。

        **返回空列表 ≠ 出错**：高德对搜不到的关键词就是返回 `pois: []`，
        调用方必须把「空」当正常业务结果处理（校验层的"搜不到"是软提示不是硬错）。
        ⚠️ 但**限流**也是返回空（`status=0` + `CUQPS_*`）—— 那个不是业务结果，
        real 实现必须识别并重试，不能让它冒充"搜不到"（风险 16）。
        """
        ...

    async def get_poi(self, poi_id: str) -> AmapPoi | None:
        """按 poi_id 精确查一个地点（`GET /spots/{poi_id}` 的数据面）。

        查不到返回 `None`（路由层转 404）。real 走 `/v3/place/detail`；
        mock 直接查池子。⚠️ 与 `search_poi` 同一条规矩：限流响应必须重试，
        不能让它冒充"查不到"。
        """
        ...

    async def get_weather(self, city: str, day: date) -> Weather:
        """查某天的天气。

        ⚠️ 高德只给**从今天起的 4 天**，没有"按指定日期查"的能力。
        所以 `day` 在窗口外时**不报错**，而是返回 `status=unavailable` + `note` 说明原因。
        这是契约的一部分（`schemas.py` 的 `WeatherStatus`），mock 也必须遵守。
        """
        ...

    async def calc_distance(
        self,
        origin: tuple[float, float],
        dest: tuple[float, float],
    ) -> DistanceResult:
        """算两点之间的驾车距离与耗时。参数与返回都是 `(lng, lat)` 顺序 —— 高德惯例。"""
        ...


__all__ = ["AmapProvider", "DistanceResult"]
