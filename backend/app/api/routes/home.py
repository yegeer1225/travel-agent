"""首页聚合接口：`GET /home`（M6 契约、M9 后半实现；2026-09-19 数据源切收录库 R1）。

契约（api.md 3.6）：
- 一次请求拿完 Hero + 猜你喜欢，前端少一个 loading 态
- `hero` 给最多 3 张；素材 = 收录库（高德真实快照）的 `photos[0]`（真数据、可追溯）
- `recommended` = 收录库评分降序前 4（排除 hero，与 `/spots` 列表同源同排序口径）
- hero 与 recommended 的景点**不重复**
- 拿不到照片就少给一张，**绝不 AI 生图 / 灰底占位**（A39）

数据源（R1，豆包 2026-09-19 转交）：**纯本地读库，零出站** —— 首页原来逐个关键词调
`provider.search_poi`（依赖高德网络/配额，且与收录列表不同源）；现在只读 `spots` 表，
高德不可用时首页照常工作。接口形状不变：`HomeResponse{hero, recommended}` 零改动。

⚠️ 不设缓存层：本地查询毫秒级（D70 同款理由——原来的进程内缓存只为省高德配额）。

公开访问（不要求登录）—— 首页在登录前就要能看。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import get_spot_repo
from app.schemas import HeroSlide, HomeResponse

router = APIRouter(tags=["home"])

_HERO_MAX = 3
_RECOMMEND_MAX = 4


@router.get("/home", response_model=HomeResponse)
async def home(request: Request) -> HomeResponse:
    cards, _total = get_spot_repo(request).list_all(limit=50)

    # hero：排序在前、且**带照片**的最多 3 个（没图不硬凑，A39）
    hero_cards = [c for c in cards if c.photos][:_HERO_MAX]
    hero_ids = {c.poi_id for c in hero_cards}

    # recommended：评分降序前 4，排除 hero（契约：两区不重复）
    rec_cards = [c for c in cards if c.poi_id not in hero_ids][:_RECOMMEND_MAX]

    return HomeResponse(
        hero=[
            # HeroSlide.city 是必填 str；收录库个别行可能为空 → 回落区县，再空就空串（不编）
            HeroSlide(poi_id=c.poi_id, name=c.name, city=c.city or c.district or "", photo=c.photos[0])
            for c in hero_cards
        ],
        recommended=rec_cards,
    )
