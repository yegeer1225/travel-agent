"""数据源工厂。

**调用方永远不 `import` 具体实现** —— 只调 `build_provider()`。
这样 M2 换成真高德时，图 / 工具 / 测试一行都不用改（D23 的兑现方式）。
"""

from __future__ import annotations

from app.config import Settings, settings as default_settings
from app.providers.base import AmapProvider, DistanceResult
from app.providers.mock import MOCK_CITY, MockAmapProvider


def build_provider(settings: Settings | None = None) -> AmapProvider:
    """按配置造数据源。

    ⚠️ `real` 分支是**延迟导入**的。原因：M1 阶段 `providers/amap.py` 还不存在，
    顶层 import 会让整个包在 M1 期间都跑不起来 —— 而 M1 的全部工作都建立在 mock 上。
    延迟导入让"M2 还没写"这件事只影响 `AMAP_PROVIDER=real` 这一条路径。
    """
    s = settings or default_settings

    if s.mock_mode:
        return MockAmapProvider()

    if not s.amap_webservice_key:
        raise RuntimeError(
            "AMAP_PROVIDER=real 但缺少 AMAP_WEBSERVICE_KEY。"
            "要么补上 Key，要么把 AMAP_PROVIDER 改回 mock"
        )

    try:
        from app.providers.amap import AmapHttpProvider
    except ImportError as exc:  # pragma: no cover - M2 落地后此分支消失
        raise RuntimeError(
            "AMAP_PROVIDER=real 需要 app/providers/amap.py，但它还没实现（排在 M2）。"
            "现在请用 AMAP_PROVIDER=mock"
        ) from exc

    return AmapHttpProvider(s.amap_webservice_key)


__all__ = ["AmapProvider", "DistanceResult", "MOCK_CITY", "MockAmapProvider", "build_provider"]
