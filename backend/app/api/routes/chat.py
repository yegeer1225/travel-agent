"""chat 接口（M6）：`POST /sessions/{id}/chat` → SSE。

═══════════════════════════════════════════════════════════════
 这条路由管什么 / 不管什么
═══════════════════════════════════════════════════════════════

**管 HTTP 的事**：归属校验（404）、限流（D28：10/h·30/天）、
同会话并发流 = 1（重复发起 → 429）、SSE 帧格式（`id:` + `data:` 单行 JSON + 空行）、
心跳（图卡住也每 14s 一条 `: ping`）、落库（用户消息 / 助手消息 + meta / 行程）。

**不管 agent 的事**：图怎么跑、LangGraph 事件怎么翻译成 SSE 事件，
全在 `chat_stream.py`。测试注入 fake 句柄时本文件原样可测（不连库不连 LLM）。

⚠️ **`error` 事件之后必发 `done`**：前端靠 done 收 loading，EOF 没 done
会被当成"断流"提示重试 —— 错误和断流必须是两种不同的前端表现。

⚠️ steer（插队）**不走限流、不占并发名额**：它不启动新的 agent 运行，
只是一次 checkpoint 状态写入 —— 把它算进 10/h 配额等于惩罚用户纠正 agent。

⚠️ 并发守卫的释放点在 `_sse_body` 的 `finally`：StreamingResponse 返回后
生成器才被消费，"请求处理函数返回"≠"流结束"。放在路由主体里释放，
守卫会在 agent 还在跑的时候就被清掉 —— 第二个请求就能挤进来。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import get_current_user_id, get_session_repo, get_trip_repo
from app.api.errors import AppError, RateLimited
from app.api.ratelimit import CHAT_DAILY, CHAT_HOURLY
from app.api.sse import SSE_HEADERS, frame as _frame  # 帧格式唯一实现点（M7 抽出，paste 复用）
from app.schemas import (
    CheckEvent,
    DoneEvent,
    ErrorEvent,
    SessionEvent,
    TokenEvent,
    ToolResultEvent,
    TripEvent,
)
from app.store.repo import DEFAULT_SESSION_TITLE, SessionRepo, TripRepo

router = APIRouter(tags=["chat"])


def _derive_title(session_title: str, trip: Any) -> str | None:
    """首轮生成行程后，把默认标题换成「成都 · 3 天」。

    只在标题还是默认值时改 —— 用户改过的标题**不覆盖**。
    """
    if session_title != DEFAULT_SESSION_TITLE:
        return None
    days = len(trip.days)
    return f"{trip.destination} · {days} 天" if days else None


async def _sse_body(
    handle: Any,
    registry: Any,
    *,
    session_id: str,
    message: str,
    resume: bool,
    has_checkpoint: bool,
    session_repo: SessionRepo,
    trip_repo: TripRepo,
    user_id: int,
) -> AsyncIterator[str]:
    """把句柄的事件流包装成 SSE 帧序列 + 流结束后的落库。

    落库放在生成器里（而不是路由主体）是因为：**只有流真的被消费完，
    行程/回复才算"发生过"** —— 客户端半途断开时（CancelledError），
    这轮的产物不落 assistant 消息（用户消息已落，重连走 Last-Event-ID 恢复）。

    助手消息的 `meta`（tool_calls / trip_id / checks）是契约的一部分：
    没有它，历史会话点开只剩几行字，轨迹卡/行程卡/校验卡全丢 ——
    "看得见 agent 在干活"是这个项目最主要的差异点，历史里丢一半等于白做。
    """
    # 第一条永远是 session（chat 与 paste 的唯一区别之一，api.md 3.7）
    yield _frame(0, SessionEvent(type="session", session_id=session_id))

    assistant_text: list[str] = []
    tool_records: list[dict[str, Any]] = []
    last_checks: list[dict[str, Any]] = []
    trip = None
    seq = 0

    try:
        try:
            async for item in handle.stream(message, resume=resume, has_checkpoint=has_checkpoint):
                if item.get("heartbeat"):
                    yield ": ping\n\n"
                    continue
                if "final_state" in item:
                    continue  # 行程已经通过 trip 事件拿到，终态兜底不需要再发

                evt = item["event"]
                if isinstance(evt, TokenEvent):
                    assistant_text.append(evt.text)
                elif isinstance(evt, ToolResultEvent):
                    tool_records.append({
                        "tool": evt.tool, "args": {}, "ok": evt.ok,
                        "summary": evt.summary, "degraded": evt.degraded,
                    })
                elif isinstance(evt, CheckEvent):
                    last_checks = [c.model_dump(mode="json") for c in evt.checks]
                elif isinstance(evt, TripEvent):
                    trip = evt.trip
                elif isinstance(evt, ErrorEvent):
                    pass  # error 帧照发；done 由下方统一补上

                seq += 1
                yield _frame(seq, evt)

            # ── 落库（只在流正常走完时执行；异常路径跳过）──
            trip_id = None
            if trip is not None:
                trip_repo.save(user_id, trip)
                trip_id = trip.trip_id
                title = _derive_title(session_repo.get(user_id, session_id).title, trip)  # type: ignore[union-attr]
                if title:
                    session_repo.update_title(user_id, session_id, title)

            content = "".join(assistant_text).strip() or "（本轮没有产出文本回复）"
            session_repo.append_message(
                user_id, session_id, role="assistant", content=content,
                meta={"tool_calls": tool_records, "trip_id": trip_id, "checks": last_checks},
            )

            seq += 1
            yield _frame(seq, DoneEvent(type="done", session_id=session_id, trip_id=trip_id))
        except Exception as exc:  # noqa: BLE001 —— 兜底防线：流不能"无声死掉"
            # 正常情况下 producer 异常已在 graph_chat_stream 里转成 error 帧；
            # 这里是**第二道防线** —— 翻译层漏了（或测试替身抛异常）时，
            # 客户端也必须拿到成对的 error+done 才能收 loading、提示重试。
            # 🔴 CancelledError 是 BaseException，不进这里：客户端断开不需要收尾帧。
            seq += 1
            yield _frame(seq, ErrorEvent(
                type="error", code="internal_error",
                msg=f"{type(exc).__name__}: {exc}"[:200],
            ))
            seq += 1
            yield _frame(seq, DoneEvent(type="done", session_id=session_id, trip_id=None))
    finally:
        registry.finish(session_id)


@router.post("/sessions/{session_id}/chat")
async def chat(
    session_id: str,
    request: Request,
    user_id: int = Depends(get_current_user_id),
    session_repo: SessionRepo = Depends(get_session_repo),
    trip_repo: TripRepo = Depends(get_trip_repo),
) -> Any:
    """对话入口。steer=True 走插队分支（JSON 响应），否则起 SSE 流。"""
    try:
        body_raw = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise AppError("invalid_param", "请求体必须是 JSON", 400) from exc

    message = str(body_raw.get("message") or "").strip()
    steer = bool(body_raw.get("steer"))
    if not message:
        raise AppError("invalid_param", "message 不能为空", 400)
    if len(message) > 2000:
        raise AppError("invalid_param", "message 最长 2000 字", 400)

    session = session_repo.get(user_id, session_id)
    if session is None:
        raise AppError("not_found", "会话不存在", 404)

    registry = request.app.state.active_chats
    limiter = request.app.state.limiter

    # ── steer：插队（不占限流/并发名额，见模块 docstring）──
    if steer:
        handle = registry.handle_of(session_id)
        if handle is None:
            raise AppError("invalid_param", "当前没有进行中的对话可插队", 400)
        session_repo.append_message(user_id, session_id, role="user", content=message)
        ok = await handle.steer(message)
        return JSONResponse({"ok": ok, "injected": True})

    # ── 正常一轮：限流（D28）→ 并发守卫 → 落用户消息 → 起流 ──
    for rule in (CHAT_HOURLY, CHAT_DAILY):
        retry_after = limiter.check(f"{rule.name}:{user_id}", rule)
        if retry_after:
            raise RateLimited(f"操作太频繁，请 {retry_after} 秒后重试", retry_after)

    if registry.is_active(session_id):
        raise RateLimited("该会话已有进行中的对话，请等它结束", 5)

    resume = "last-event-id" in request.headers  # D27：带 Last-Event-ID = 断线恢复

    handle = await request.app.state.chat_factory(session_id)
    if not resume:  # 恢复时不重发用户消息（checkpoint 里已经有了）
        session_repo.append_message(user_id, session_id, role="user", content=message)
    has_checkpoint = await handle.has_checkpoint()

    registry.start(session_id, handle)
    try:
        stream = _sse_body(
            handle, registry,
            session_id=session_id, message=message, resume=resume, has_checkpoint=has_checkpoint,
            session_repo=session_repo, trip_repo=trip_repo, user_id=user_id,
        )
        return StreamingResponse(stream, media_type="text/event-stream", headers=SSE_HEADERS)
    except BaseException:
        registry.finish(session_id)
        raise


__all__ = ["router"]
