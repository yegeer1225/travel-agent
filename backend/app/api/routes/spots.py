"""景点搜索接口（M4 契约、M9 后半实现）：`GET /spots/search`。

🔴 三条契约（api.md 3.5）：
- `keywords` 为空 → 400 `keyword_required`（前端空关键词时根本不该发请求）
- 响应带 `source`（amap/mock）与 `cached`（命中进程内缓存）
- 高德 QPS 限流的串行 + 退避在 provider 层做（D35），**这里再叠一层进程内缓存**
  —— 搜索页翻页/重复搜索不该反复烧高德配额。

限流 120/分钟·用户（D28）：limiter 实例挂在 app.state 上，不能做成模块级
router dependency —— 所以在路由体内手动查（与 chat 的手动限流同一模式）。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_current_user_id, get_limiter, get_nodes
from app.api.errors import AppError, RateLimited
from app.api.ratelimit import SPOT_SEARCH_PER_MIN
from app.schemas import SpotCard, SpotSearchResponse

router = APIRouter(tags=["spots"])

_CACHE_TTL_SECONDS = 300.0
# key=(keywords, city) → (monotonic 时间, POI 列表)。进程内缓存：重启即失效，够用。
# ⚠️ 只按 (keywords, city) 缓存**整批**（最多 50 条），分页在内存里切 —— 高德配额最省。
_cache: dict[tuple[str, str], tuple[float, list]] = {}


def _to_spot_card(poi) -> SpotCard:
    """AmapPoi → SpotCard（对外精简卡，全量透传会把内部结构变成对外契约）。"""
    return SpotCard(
        poi_id=poi.poi_id,
        name=poi.name,
        city=poi.cityname,
        district=poi.adname,
        address=poi.address,
        lng=poi.lng,
        lat=poi.lat,
        cost_per_person=poi.cost_per_person,
        rating=poi.rating,
        photos=list(poi.photos or []),
        typecode=poi.typecode,
    )


@router.get("/spots/search", response_model=SpotSearchResponse)
async def search_spots(
    request: Request,
    keywords: str = Query(..., description="搜索关键词，为空 400"),
    city: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    user_id: int = Depends(get_current_user_id),
) -> SpotSearchResponse:
    limiter = get_limiter(request)
    retry_after = limiter.check(f"{SPOT_SEARCH_PER_MIN.name}:{user_id}", SPOT_SEARCH_PER_MIN)
    if retry_after:
        raise RateLimited(f"操作太频繁，请 {retry_after} 秒后重试", retry_after)

    kw = keywords.strip()
    if not kw:
        raise AppError("keyword_required", "请输入搜索关键词", 400)

    nodes = get_nodes(request)  # 懒建运行时；provider 按 settings 的 mock/real 路由
    provider = nodes.provider

    cache_key = (kw, city or "")
    now = time.monotonic()
    cached = False
    hit = _cache.get(cache_key)
    if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
        cached = True
        all_pois = hit[1]
    else:
        all_pois = await provider.search_poi(kw, city=city, limit=50)
        _cache[cache_key] = (now, all_pois)

    page = all_pois[offset : offset + limit]
    return SpotSearchResponse(
        items=[_to_spot_card(p) for p in page],
        total=offset + len(all_pois),
        source="mock" if getattr(provider, "name", "real") == "mock" else "amap",
        cached=cached,
    )
