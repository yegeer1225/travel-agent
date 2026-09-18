"""景点接口：`GET /spots`（列表）+ `GET /spots/search` + `GET /spots/{poi_id}`。

═══════════════════════════════════════════════════════════════
 2026-09-17 语义变更（D70）：景点页搜的是**本站收录库**，不再代理高德
═══════════════════════════════════════════════════════════════

| 接口 | 数据来源 |
|---|---|
| `GET /spots` | **只有收录库**（`spots` 表）全量分页 —— 景点页默认态平铺用（2026-09-18） |
| `GET /spots/search` | **只有收录库**（`spots` 表）。收录库里没有 → 空列表，**不回落**（D70） |
| `GET /spots/{poi_id}` | **收录库优先 → 回落 provider → 都没有 404** |

**为什么 search 不回落**：景点页的语义是"搜本站收录的景点"。回落会把同一页混成
两种来源（要前端解释"这条为什么来自高德"），且把"库里确实没有"这条真实路径掩盖掉 ——
和 D67「池空要分因」一个道理。

**为什么 detail 要回落**：它是"按 id 拿一条"，不是"搜索"。行程卡片、收藏、攻略里的
`poi_id` 可能指向**不在收录库**的地点（那些是高德侧的 id），此时 404 会显得像 bug。

三条契约（api.md 3.5）：
- `keywords` 为空 → 400 `keyword_required`（前端空关键词时根本不该发请求）
- 搜索响应带 `source`（恒 `local`）与 `cached`（本地路径恒 `false`）；**列表响应没有这两个字段**
- 限流 120/分钟·用户（D28）**保留**：它原来守的是高德配额，现在守的是库
  （便宜不等于可以无限刷，且改阈值是另一条决策，不夹带在这次变更里）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_current_user_id, get_limiter, get_nodes, get_spot_repo
from app.api.errors import AppError, RateLimited
from app.api.ratelimit import SPOT_SEARCH_PER_MIN
from app.schemas import SpotCard, SpotListResponse, SpotSearchResponse

router = APIRouter(tags=["spots"])


def _to_spot_card(poi) -> SpotCard:
    """AmapPoi → SpotCard（对外精简卡，全量透传会把内部结构变成对外契约）。

    只在**详情回落 provider** 那条路上用到 —— 搜索走收录库，store 层直接返回 SpotCard。
    """
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


@router.get("/spots", response_model=SpotListResponse)
async def list_spots(
    request: Request,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    user_id: int = Depends(get_current_user_id),
) -> SpotListResponse:
    """收录库全量分页（景点页默认态平铺，2026-09-18）。

    与 search 共用同一条限流规则：都是毫秒级本地读，成本同级，
    不为它发明 D28 表里没有的新阈值。排序 = 评分降序 → 名称（可解释，见 repo）。
    """
    limiter = get_limiter(request)
    retry_after = limiter.check(f"{SPOT_SEARCH_PER_MIN.name}:{user_id}", SPOT_SEARCH_PER_MIN)
    if retry_after:
        raise RateLimited(f"操作太频繁，请 {retry_after} 秒后重试", retry_after)

    items, total = get_spot_repo(request).list_all(limit=limit, offset=offset)
    return SpotListResponse(items=items, total=total)


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

    items, total = get_spot_repo(request).search(kw, city=city, limit=limit, offset=offset)
    return SpotSearchResponse(items=items, total=total, source="local", cached=False)


@router.get("/spots/{poi_id}", response_model=SpotCard)
async def get_spot(
    request: Request,
    poi_id: str,
    user_id: int = Depends(get_current_user_id),
) -> SpotCard:
    """收录库优先 → provider 回落 → 404（`not_found`，与全局 404 语义一致）。"""
    card = get_spot_repo(request).get(poi_id)
    if card is not None:
        return card

    poi = await get_nodes(request).provider.get_poi(poi_id)
    if poi is None:
        raise AppError("not_found", "景点不存在", 404)
    return _to_spot_card(poi)
