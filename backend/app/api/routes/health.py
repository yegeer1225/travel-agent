"""探活接口。`api.md` 2.1：联调第一步就是打它。"""

from __future__ import annotations

from fastapi import APIRouter

from app.config import settings

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "mock_mode": settings.mock_mode,
        "model_tool": settings.llm_model_tool,
        "model_plan": settings.llm_model_plan,
        "amap_configured": bool(settings.amap_webservice_key),
        # D66：两个档一起给，才能区分「主动用 mock」和「缺 Key 被降级」
        "amap_provider_requested": settings.amap_provider_requested or settings.amap_provider,
        "amap_degraded": settings.amap_degraded,
    }
