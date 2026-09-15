"""工具层：3 个模型可调的工具 + 封闭世界校验的 POI 池。"""

from app.tools.poi_pool import PoiPool, current_pool, poi_pool_scope, record

# ⚠️ 顺序不能反：`amap_tools` 内部 `from app.tools import poi_pool`，
# 先导入 `poi_pool` 让它先进 `sys.modules`，避免包初始化期的循环导入。
from app.tools.amap_tools import MAX_CANDIDATES, build_amap_tools

__all__ = [
    "MAX_CANDIDATES",
    "PoiPool",
    "build_amap_tools",
    "current_pool",
    "poi_pool_scope",
    "record",
]
