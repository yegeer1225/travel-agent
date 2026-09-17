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

    ⚠️ `real` 分支是**延迟导入**的。历史原因：M1 阶段 `providers/amap.py` 还不存在，
    顶层 import 会让整个包在 M1 期间都跑不起来 —— 而 M1 的全部工作都建立在 mock 上。
    M2 落地后该文件已存在，延迟导入继续保留：不用 `real` 的路径**完全不加载**高德那套代码。
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
    except ImportError as exc:  # pragma: no cover - 兜底分支
        raise RuntimeError(
            "AMAP_PROVIDER=real 需要 app/providers/amap.py 能导入，但导入失败了。"
            f"检查该文件是否存在、依赖是否装全（httpx）。原始错误：{exc}"
        ) from exc

    return AmapHttpProvider(s.amap_webservice_key)


__all__ = ["AmapProvider", "DistanceResult", "MOCK_CITY", "MockAmapProvider", "build_provider"]
