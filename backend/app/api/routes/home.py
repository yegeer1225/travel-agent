"""首页聚合接口：`GET /home`（M6 契约、M9 后半实现）。

契约（api.md 3.6）：
- 一次请求拿完 Hero + 猜你喜欢，前端少一个 loading 态
- `hero` 给 3~5 张；素材 = 高德 POI 的 `photos[0]`（真数据、可追溯）
- hero 与 recommended 的景点**不重复**
- 拿不到照片就少给一张，**绝不 AI 生图 / 灰底占位**（A39）

公开访问（不要求登录）—— 首页在登录前就要能看。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

from app.api.deps import get_nodes
from app.schemas import HeroSlide, HomeResponse, SpotCard

router = APIRouter(tags=["home"])

# 首页展示用的关键词池（前几个给 hero，其余给 recommended）。
# 每个词取搜索第一名；搜不到就跳过 —— **不硬凑**（A39）。
# ⚠️ 关键词选择兼顾 mock 池（providers/mock.py 9 个成都 POI）与真实高德，
#     两边都能命中，演示与上线行为一致。
_HERO_KEYWORDS = ("宽窄巷子", "武侯祠", "成都大熊猫繁育研究基地", "人民公园")
_RECOMMEND_KEYWORDS = ("锦里古街", "成都太古里", "春熙路步行街", "都江堰景区")

_HOME_CACHE_TTL = 600.0
_home_cache: tuple[float, HomeResponse] | None = None


def _to_spot_card(poi) -> SpotCard:
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


@router.get("/home", response_model=HomeResponse)
async def home(request: Request) -> HomeResponse:
    global _home_cache
    now = time.monotonic()
    if _home_cache is not None and now - _home_cache[0] < _HOME_CACHE_TTL:
        return _home_cache[1]

    provider = get_nodes(request).provider

    hero_pois: list = []
    rec_pois: list = []
    seen: set[str] = set()

    async def _first(keyword: str):
        """关键词 → 第一个没见过的 POI；搜不到/重复 = 跳过（不硬凑）。"""
        results = await provider.search_poi(keyword, limit=3)
        for poi in results:
            if poi.poi_id not in seen:
                seen.add(poi.poi_id)
                return poi
        return None

    for kw in _HERO_KEYWORDS:
        poi = await _first(kw)
        if poi is not None and poi.photos:
            hero_pois.append(poi)
    for kw in _RECOMMEND_KEYWORDS:
        poi = await _first(kw)
        if poi is not None:
            rec_pois.append(poi)

    resp = HomeResponse(
        hero=[
            HeroSlide(poi_id=p.poi_id, name=p.name, city=p.cityname or "成都", photo=p.photos[0])
            for p in hero_pois[:5]
        ],
        recommended=[_to_spot_card(p) for p in rec_pois[:4]],
    )
    _home_cache = (now, resp)
    return resp
