"""行程接口：M5 列表/详情 + M7 paste（SSE）/ PATCH（确定性重算）/ recheck。

═══════════════════════════════════════════════════════════════
 四个端点的成本天差地别，限流按 D28 分层
═══════════════════════════════════════════════════════════════

| 端点 | LLM | 高德 | 限流 |
|---|---|---|---|
| GET 列表/详情 | 无 | 无 | 仅全局兜底 |
| PATCH | **无**（确定性重算） | 逐站测距 | 全局兜底（幂等，误伤无害） |
| recheck | **有**（软校验一次） | 无 | 60/小时 |
| paste | 有（解析+软校验） | 逐站搜索 | 20/小时 |

paste 的 SSE 帧格式与 chat 完全同款（`app/api/sse.py` 唯一实现点），
差别只有两条：**不发 `session` 帧**、`done.session_id` 为 `null`（A37）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user_id, get_nodes, get_trip_repo
from app.api.errors import AppError, RateLimited
from app.api.ratelimit import RECHECK_HOURLY, PASTE_HOURLY
from app.api.sse import SSE_HEADERS, frame
from app.graph.intent import Requirements
from app.graph.paste import paste_trip_stream
from app.graph.recompute import OpApplyError, apply_ops, recompute_trip
from app.graph.soft import run_soft_checks
from app.graph.validate import flatten_checks
from app.schemas import (
    AmapImportResponse,
    CheckLevel,
    CheckStatus,
    DoneEvent,
    ErrorEvent,
    Page,
    PasteTripRequest,
    PatchTripRequest,
    RecheckResponse,
    Trip,
    TripEvent,
    TripSummaryItem,
    Validation,
    ValidationIssue,
)
from app.store.repo import TripRepo

router = APIRouter(tags=["trips"])


@router.get("/trips")
def list_trips(
    user_id: int = Depends(get_current_user_id),
    repo: TripRepo = Depends(get_trip_repo),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Page[TripSummaryItem]:
    """我的行程列表（按 created_at 倒序）—— 个人中心「我的 AI 路线规划记录」用它。"""
    items, total = repo.list(user_id, limit=limit, offset=offset)
    return Page[TripSummaryItem](items=items, total=total, limit=limit, offset=offset)


@router.get("/trips/{trip_id}")
def get_trip(
    trip_id: str,
    user_id: int = Depends(get_current_user_id),
    repo: TripRepo = Depends(get_trip_repo),
) -> Trip:
    """完整 Trip（含 days / stops / checks / summary）。

    归属校验在 repo 的 WHERE 里（`user_id` 是第一个位置参数，D31 防线）——
    别人的 trip_id 在这里**查询不到**，自然落进 404，不会 403。
    """
    trip = repo.get(user_id, trip_id)
    if trip is None:
        raise AppError("not_found", "行程不存在", 404)
    return trip


# ══════════════════════════════════════════════════════════════
#  M7：PATCH —— 确定性重算（不调 LLM）
# ══════════════════════════════════════════════════════════════


@router.patch("/trips/{trip_id}")
async def patch_trip(
    trip_id: str,
    body: PatchTripRequest,
    user_id: int = Depends(get_current_user_id),
    repo: TripRepo = Depends(get_trip_repo),
    nodes=Depends(get_nodes),
) -> Trip:
    """界面二的确定性改动：一批 op 一次提交 → 一次重算 → 返回**完整 Trip**。

    响应不是 diff —— 前端**整体替换**本地 state（api.md 3.2），
    一次提交/一次重算/一次替换，界面上永远看不到"时间还没重排完"的中间态。
    """
    trip = repo.get(user_id, trip_id)
    if trip is None:
        raise AppError("not_found", "行程不存在", 404)

    try:
        weather_days, pinned = apply_ops(trip, body.ops)
    except OpApplyError as exc:
        raise AppError("invalid_param", str(exc), 400) from exc

    await recompute_trip(
        trip,
        provider=nodes.provider,
        requirements=Requirements().with_defaults(),
        weather_days=weather_days,
        pinned_arrive=pinned,
    )
    repo.save(user_id, trip)  # save 是覆盖语义（ON DUPLICATE KEY UPDATE）
    return trip


# ══════════════════════════════════════════════════════════════
#  M7：recheck —— 深度软校验（唯一调 LLM 的行程级接口）
# ══════════════════════════════════════════════════════════════


@router.post("/trips/{trip_id}/recheck")
async def recheck_trip(
    trip_id: str,
    request: Request,
    user_id: int = Depends(get_current_user_id),
    repo: TripRepo = Depends(get_trip_repo),
    nodes=Depends(get_nodes),
) -> RecheckResponse:
    """重跑软判据（一次 LLM，5~15s）。**返回的是校验卡不是完整 Trip** ——
    重排是 agent 的事，前端只更新校验卡和角标（api.md 3.3）。"""
    limiter = request.app.state.limiter
    retry_after = limiter.check(f"{RECHECK_HOURLY.name}:{user_id}", RECHECK_HOURLY)
    if retry_after:
        raise RateLimited(f"操作太频繁，请 {retry_after} 秒后重试", retry_after)

    trip = repo.get(user_id, trip_id)
    if trip is None:
        raise AppError("not_found", "行程不存在", 404)

    req = Requirements().with_defaults()
    await run_soft_checks(trip, req, nodes.llm_soft)

    # 软判据挂在**三层**（apply_soft_findings 按 LEVEL_OF 分配：站级 / 天级 / 行程级），
    # 必须扫全树 —— 只扫 trip.checks 会漏掉站级的 needs_booking / queue_time
    # （2026-09-16 端到端实测抓到：LLM 判了两条站级 fail，remaining 却是空的）。
    # 用 flatten_checks 与响应里的 checks 保证同源。
    soft_fails = [
        c for c in flatten_checks(trip.model_dump(mode="json"))
        if c.level is CheckLevel.SOFT and c.status is CheckStatus.FAILED
    ]
    trip.summary.soft_warnings = len(soft_fails)
    # rounds=0：recheck 不重排（没有 agent 参与），只是重新提醒
    validation = Validation(
        rounds=0, fixed=[],
        remaining=[
            ValidationIssue(code=c.code, msg=c.msg or "", level=CheckLevel.SOFT)
            for c in soft_fails
        ],
    )
    trip.validation = validation
    repo.save(user_id, trip)

    return RecheckResponse(
        trip_id=trip.trip_id,
        checks=flatten_checks(trip.model_dump(mode="json")),
        validation=validation,
    )


# ══════════════════════════════════════════════════════════════
#  M7：paste —— 热启动 SSE（不建会话，A37）
# ══════════════════════════════════════════════════════════════


async def _paste_sse_body(
    flow: AsyncIterator[dict], repo: TripRepo, user_id: int
) -> AsyncIterator[str]:
    """paste 的事件流包装：转发帧 → 流结束落库 → done（session_id=null）。

    与 chat 的 `_sse_body` 同构但更简单：没有会话（无首帧/无消息落库）、
    无并发守卫（20/h 限流已兜住滥用）。error 后必发 done 的铁律相同。
    """
    seq = 0
    trip = None
    try:
        async for item in flow:
            if item.get("heartbeat"):
                yield ": ping\n\n"
                continue
            evt = item["event"]
            from app.schemas import TripEvent as _TripEvent

            if isinstance(evt, _TripEvent):
                trip = evt.trip
            seq += 1
            yield frame(seq, evt)

        trip_id = None
        if trip is not None:
            repo.save(user_id, trip)
            trip_id = trip.trip_id
        seq += 1
        yield frame(seq, DoneEvent(type="done", session_id=None, trip_id=trip_id))
    except Exception as exc:  # noqa: BLE001 —— 第二道防线，与 chat 同款
        seq += 1
        yield frame(seq, ErrorEvent(
            type="error", code="internal_error",
            msg=f"{type(exc).__name__}: {exc}"[:200],
        ))
        seq += 1
        yield frame(seq, DoneEvent(type="done", session_id=None, trip_id=None))


@router.post("/trips/paste")
async def paste_trip(
    body: PasteTripRequest,
    request: Request,
    user_id: int = Depends(get_current_user_id),
    repo: TripRepo = Depends(get_trip_repo),
    nodes=Depends(get_nodes),
) -> StreamingResponse:
    """热启动：粘一段行程文本 → 解析 → 逐站搜真 POI → 校验 → SSE。"""
    limiter = request.app.state.limiter
    retry_after = limiter.check(f"{PASTE_HOURLY.name}:{user_id}", PASTE_HOURLY)
    if retry_after:
        raise RateLimited(f"操作太频繁，请 {retry_after} 秒后重试", retry_after)

    flow = paste_trip_stream(nodes, text=body.text, destination=body.destination, user_id=user_id)
    return StreamingResponse(
        _paste_sse_body(flow, repo, user_id),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/trips/{trip_id}/amap-import", response_model=AmapImportResponse)
def amap_import(
    trip_id: str,
    user_id: int = Depends(get_current_user_id),
    repo: TripRepo = Depends(get_trip_repo),
) -> AmapImportResponse:
    """高德 APP 唤端链接（M7 🔵）：把行程站点拼成 uri.amap.com 的导航链接。

    这是**唤端链接不是数据写入** —— 高德没有"导入行程"的开放 API，
    能给的就是一条多途经点导航 URL，用户点开在 App 里看路线。
    途经点上限 9 个是链接长度与高德解析的稳妥边界，超出的写进 note 说明。
    """
    import urllib.parse

    trip = repo.get(user_id, trip_id)
    if trip is None:
        raise AppError("not_found", "行程不存在", 404)

    pts: list[tuple[float, float, str]] = []
    for day in trip.days:
        for stop in sorted(day.stops, key=lambda s: s.seq):
            pts.append((stop.lng, stop.lat, stop.name))
    if not pts:
        raise AppError("invalid_state", "行程没有任何站点，无法生成高德链接", 400)

    def _pt(p: tuple[float, float, str]) -> str:
        return f"{p[0]:.6f},{p[1]:.6f},{urllib.parse.quote(p[2])}"

    via = pts[1:-1]
    note: str | None = None
    if len(via) > 9:
        note = f"途经点共 {len(via)} 个，链接只带前 9 个，其余请在高德内手动补齐"
        via = via[:9]

    params = {
        "from": _pt(pts[0]),
        "to": _pt(pts[-1]),
        "mode": "car",
        "src": "trip-agent",
        "coordinate": "gaode",
        "callnative": "0",
    }
    if via:
        params["via"] = ";".join(_pt(p) for p in via)
    url = "https://uri.amap.com/navigation?" + urllib.parse.urlencode(params)
    return AmapImportResponse(url=url, note=note)
