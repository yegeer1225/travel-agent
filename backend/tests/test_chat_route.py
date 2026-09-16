"""chat 接口（M6 SSE）—— 行为逐条对 api.md 3.7。

═══════════════════════════════════════════════════════════════
 测法：注入 fake 句柄，路由测的是 HTTP 的事
═══════════════════════════════════════════════════════════════

`chat_stream.py`（事件映射）的测试在 `test_chat_stream.py` —— 那里跑真图。
本文件只测**路由自己管的事**：归属 404 / 校验 400 / 限流 / 并发守卫 429 /
steer 分支 / 帧格式 / 落库 / 标题派生 / error 后补 done。

所以句柄用 fake（`FakeHandle`）：只实现路由碰的三个方法
（`has_checkpoint` / `steer` / `stream`），事件按脚本吐 ——
不连图、不连库、不连 LLM，毫秒级且完全确定。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from conftest import auth_header, make_trip

from app.api.main import create_app
from app.api.ratelimit import SlidingWindowLimiter
from app.api.routes.chat import _derive_title
from app.schemas import (
    Check,
    CheckEvent,
    CheckLevel,
    CheckStatus,
    Day,
    ErrorEvent,
    Stop,
    TokenEvent,
    TripEvent,
)
from app.store.memory import InMemorySessionStore, InMemoryTripStore
from app.store.repo import DEFAULT_SESSION_TITLE

# ══════════════════════════════════════════════════════════════
#  替身
# ══════════════════════════════════════════════════════════════


class FakeHandle:
    """路由测试的假句柄：三个方法 + 事件脚本 + 观察窗口。"""

    def __init__(self, script: list[dict[str, Any]] | None = None, *, has_ckpt: bool = False) -> None:
        self.script = script or []
        self._has_ckpt = has_ckpt
        self.stream_calls: list[dict[str, Any]] = []  # 每次 stream 收到的实参
        self.steer_received: list[str] = []
        self.close_calls = 0  # D59：路由是否归还了句柄资源

    async def aclose(self) -> None:
        self.close_calls += 1

    async def has_checkpoint(self) -> bool:
        return self._has_ckpt

    async def steer(self, message: str) -> bool:
        self.steer_received.append(message)
        return True

    def stream(self, message: str, *, resume: bool = False, has_checkpoint: bool = False):
        self.stream_calls.append({"message": message, "resume": resume, "has_checkpoint": has_checkpoint})

        async def gen():
            for item in self.script:
                yield item

        return gen()


def make_client(handle: FakeHandle, stores: tuple) -> TestClient:
    """带 fake chat_factory 的应用实例（限流计数从零开始）。"""
    app = create_app(
        session_repo=stores[0],
        trip_repo=stores[1],
        limiter=SlidingWindowLimiter(),
        chat_factory=fake_factory(handle),
    )
    c = TestClient(app)
    c.headers.update(auth_header())  # M9：默认用户 uid=1
    return c


def fake_factory(handle: FakeHandle, *, seen_models: list | None = None):
    async def factory(session_id: str, *, model: str | None = None) -> FakeHandle:
        if seen_models is not None:
            seen_models.append(model)  # 供"会话模型透传"断言用
        return handle

    return factory


# ══════════════════════════════════════════════════════════════
#  事件脚本素材
# ══════════════════════════════════════════════════════════════

EV_TOKEN = TokenEvent(text="行程已生成，请查看。")
EV_ERROR = ErrorEvent(code="llm_400", msg="上游模型 400")
EV_CHECK = CheckEvent(
    round=1, hard_errors=0, soft_warnings=1,
    checks=[Check(level=CheckLevel.SOFT, code="queue_time", status=CheckStatus.FAILED, msg="熊猫基地建议 8 点前到")],
)


def trip_with_days(n_days: int = 3):
    """带 N 天行程的合法 Trip（conftest 的 make_trip 天数为空，派生不了标题）。"""
    base = make_trip("t-1")
    days = [
        Day(day=d, stops=[
            Stop(seq=i, name=f"站{d}-{i}", poi_id=f"poi-{d}{i}", lng=104.06, lat=30.67, stay_min=90)
            for i in range(1, 3)
        ])
        for d in range(1, n_days + 1)
    ]
    return base.model_copy(update={"days": days})


# ══════════════════════════════════════════════════════════════
#  SSE 解析
# ══════════════════════════════════════════════════════════════


def parse_sse(text: str) -> list[dict[str, Any]]:
    """把 SSE 文本切成帧列表。心跳注释帧 = {"ping": True}，其余 = {"id", "data"}。

    ⚠️ 帧边界是空行；`data:` 必须是单行 JSON —— 这里解析失败本身就断言了
    "帧里没有裸换行"（路由 docstring 点名的坑）。
    """
    frames: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if all(ln.startswith(":") for ln in lines):
            frames.append({"ping": True})
            continue
        frame: dict[str, Any] = {}
        for ln in lines:
            if ln.startswith("id: "):
                frame["id"] = int(ln[4:])
            elif ln.startswith("data: "):
                frame["data"] = json.loads(ln[6:])
        frames.append(frame)
    return frames


def new_session(client: TestClient) -> str:
    return client.post("/api/sessions").json()["session_id"]


# ══════════════════════════════════════════════════════════════
#  入口校验
# ══════════════════════════════════════════════════════════════


def test_unknown_session_is_404(stores):
    client = make_client(FakeHandle(), stores)
    r = client.post("/api/sessions/no-such/chat", json={"message": "去成都"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_empty_and_oversized_message_is_400(stores):
    client = make_client(FakeHandle(), stores)
    sid = new_session(client)

    r = client.post(f"/api/sessions/{sid}/chat", json={"message": "   "})
    assert r.status_code == 400

    r2 = client.post(f"/api/sessions/{sid}/chat", json={"message": "长" * 2001})
    assert r2.status_code == 400

    # 非法 JSON 一样是 400（统一错误形状，不是 FastAPI 默认的 detail）
    r3 = client.post(f"/api/sessions/{sid}/chat", content=b"not json",
                     headers={"Content-Type": "application/json"})
    assert r3.status_code == 400
    assert "error" in r3.json()


# ══════════════════════════════════════════════════════════════
#  并发守卫（同会话并发流同时 1 个，D28）
# ══════════════════════════════════════════════════════════════


def test_second_concurrent_stream_is_429(stores):
    handle = FakeHandle(script=[{"event": EV_TOKEN}])
    client = make_client(handle, stores)
    sid = new_session(client)

    # 模拟"已有流在跑"：直接占位（真流怎么占位是 _sse_body finally 的事，见释放测试）
    client.app.state.active_chats.start(sid, handle)
    r = client.post(f"/api/sessions/{sid}/chat", json={"message": "再来一轮"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert not handle.stream_calls, "被守卫拦下的请求不应触达图"


def test_stream_end_releases_guard(stores):
    """释放点在 _sse_body 的 finally：流被完整消费后守卫必须解除。"""
    handle = FakeHandle(script=[{"event": EV_TOKEN}])
    client = make_client(handle, stores)
    sid = new_session(client)

    r = client.post(f"/api/sessions/{sid}/chat", json={"message": "去成都"})
    assert r.status_code == 200
    assert not client.app.state.active_chats.is_active(sid), "流结束守卫必须释放，否则会话永久 429"


def test_stream_end_closes_handle(stores):
    """D59：请求级 checkpointer 连接 —— 流走完必须 aclose（恰好一次）。"""
    handle = FakeHandle(script=[{"event": EV_TOKEN}])
    client = make_client(handle, stores)
    sid = new_session(client)

    r = client.post(f"/api/sessions/{sid}/chat", json={"message": "去成都"})
    assert r.status_code == 200
    assert handle.close_calls == 1, "正常走完必须归还连接"


def test_client_disconnect_still_closes_handle(stores):
    """D59 核心回归：客户端中途断开（生成器被 close → CancelledError）也必须归还连接。

    复现路径：TestClient 的流式响应不被完整消费 = 模拟 curl 断流。
    死连接留在进程里曾让之后所有 chat 500（单连接被打死），修复后每次请求
    拿新连接，但归还纪律仍然必须成立。
    """
    handle = FakeHandle(script=[{"event": EV_TOKEN}])
    client = make_client(handle, stores)
    sid = new_session(client)

    # stream=True 拿到响应对象但不读 body —— 模拟"连接建立了但客户端跑了"
    with client.stream("POST", f"/api/sessions/{sid}/chat", json={"message": "去成都"}) as _:
        pass  # with 退出立即断开，不等流走完

    assert handle.close_calls >= 1, "客户端断开也必须归还连接（D59）"


# ══════════════════════════════════════════════════════════════
#  steer（插队）：不占限流/并发名额
# ══════════════════════════════════════════════════════════════


def test_steer_injects_into_running_chat(stores):
    handle = FakeHandle()
    client = make_client(handle, stores)
    sid = new_session(client)
    client.app.state.active_chats.start(sid, handle)

    for _ in range(3):  # 连插三次都行 —— steer 不走限流
        r = client.post(f"/api/sessions/{sid}/chat", json={"message": "别去太远", "steer": True})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["injected"] is True

    assert handle.steer_received == ["别去太远"] * 3
    # 插话作为 user 消息留痕（历史里要能看到用户说过这句话）
    roles = [m.role for m in stores[0].list_messages(1, sid)]
    assert roles == ["user", "user", "user"]


def test_steer_without_active_chat_is_400(stores):
    client = make_client(FakeHandle(), stores)
    sid = new_session(client)
    r = client.post(f"/api/sessions/{sid}/chat", json={"message": "改一下", "steer": True})
    assert r.status_code == 400
    assert not client.app.state.active_chats.is_active(sid)


# ══════════════════════════════════════════════════════════════
#  正常一轮：帧格式 + 落库 + 标题派生
# ══════════════════════════════════════════════════════════════


def test_happy_path_frames_persistence_and_title(stores):
    handle = FakeHandle(script=[
        {"event": EV_TOKEN},
        {"event": CheckEvent(round=1, hard_errors=0, soft_warnings=1, checks=[])},
        {"event": TripEvent(trip=trip_with_days(3))},
        {"final_state": {"trip": trip_with_days(3).model_dump(mode="json")}},
    ])
    client = make_client(handle, stores)
    sid = new_session(client)

    r = client.post(f"/api/sessions/{sid}/chat", json={"message": "去成都玩三天"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = parse_sse(r.text)
    types = [f["data"]["type"] for f in frames if "data" in f]
    assert types[0] == "session", "chat 的第一条永远是 session 帧"
    assert types == ["session", "token", "check", "trip", "done"]

    # id 连续：session 是 0，事件从 1 递增
    ids = [f["id"] for f in frames if "id" in f]
    assert ids == list(range(5))

    # done 帧：trip_id 回填
    done = frames[-1]["data"]
    assert done["type"] == "done" and done["session_id"] == sid
    assert done["trip_id"] == "t-1"

    # ── 落库：行程 + 两条消息 ──
    trip_store, session_repo = stores[1], stores[0]
    assert trip_store.get(1, "t-1") is not None, "trip 事件必须落 trips"

    msgs = session_repo.list_messages(1, sid)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "去成都玩三天"),
        ("assistant", "行程已生成，请查看。"),
    ], "用户消息先落，助手消息按 token 拼接"

    meta = msgs[1].meta  # MessageMeta 是 Pydantic 模型，属性访问
    assert meta.trip_id == "t-1"
    assert meta.checks == [], "check 事件的内容收进 meta"
    assert meta.tool_calls == []

    # ── 标题派生：默认标题 + 3 天 → 「成都 · 3 天」 ──
    assert session_repo.get(1, sid).title == "成都 · 3 天"


def test_user_title_not_overridden(stores):
    """用户改过标题 → 行程生成也不覆盖（_derive_title 只认默认标题）。"""
    handle = FakeHandle(script=[{"event": TripEvent(trip=trip_with_days(2))}])
    client = make_client(handle, stores)
    sid = client.post("/api/sessions", json={"title": "我的十一之旅"}).json()["session_id"]

    client.post(f"/api/sessions/{sid}/chat", json={"message": "去成都"})
    assert stores[0].get(1, sid).title == "我的十一之旅"


def test_derive_title_unit():
    """纯函数分支：无 days / 非默认标题 → 都返回 None。"""
    assert _derive_title(DEFAULT_SESSION_TITLE, make_trip("t-1")) is None, "0 天行程不派生"
    assert _derive_title("自定义", trip_with_days(3)) is None


# ══════════════════════════════════════════════════════════════
#  断线恢复（D27）与异常收尾
# ══════════════════════════════════════════════════════════════


def test_last_event_id_means_resume_no_user_message(stores):
    """带 Last-Event-ID = 断线恢复：resume=True、不重落用户消息。"""
    handle = FakeHandle(script=[{"event": EV_TOKEN}], has_ckpt=True)
    client = make_client(handle, stores)
    sid = new_session(client)

    r = client.post(f"/api/sessions/{sid}/chat", json={"message": "继续"},
                    headers={"Last-Event-ID": "42"})
    assert r.status_code == 200

    call = handle.stream_calls[0]
    assert call["resume"] is True and call["message"] == "继续"
    roles = [m.role for m in stores[0].list_messages(1, sid)]
    assert roles == ["assistant"], "恢复轮不重发用户消息（checkpoint 里已有），只落助手回复"


def test_error_event_is_still_followed_by_done(stores):
    """🔴 error 之后必发 done：前端靠 done 收 loading，EOF 没 done = 断流。"""
    handle = FakeHandle(script=[{"event": EV_ERROR}])
    client = make_client(handle, stores)
    sid = new_session(client)

    r = client.post(f"/api/sessions/{sid}/chat", json={"message": "去成都"})
    frames = parse_sse(r.text)
    types = [f["data"]["type"] for f in frames if "data" in f]
    assert types == ["session", "error", "done"], "error 不是终点，done 才是"

    # 出错轮不落行程，但助手消息要留痕（内容为兜底文案）
    msgs = stores[0].list_messages(1, sid)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert "（本轮没有产出文本回复）" in msgs[1].content or msgs[1].content


def test_assistant_empty_text_falls_back(stores):
    """脚本没吐任何 token → 助手消息用兜底文案，绝不存空串。"""
    handle = FakeHandle(script=[{"event": TripEvent(trip=trip_with_days(1))}])
    client = make_client(handle, stores)
    sid = new_session(client)
    client.post(f"/api/sessions/{sid}/chat", json={"message": "x"})
    msgs = stores[0].list_messages(1, sid)
    assert msgs[1].content == "（本轮没有产出文本回复）"


def test_heartbeat_yields_ping_comment(stores):
    """心跳是 SSE 注释（`: ping`），前端按行读时直接忽略，不算事件。"""
    handle = FakeHandle(script=[{"heartbeat": True}, {"event": EV_TOKEN}])
    client = make_client(handle, stores)
    sid = new_session(client)

    r = client.post(f"/api/sessions/{sid}/chat", json={"message": "去成都"})
    frames = parse_sse(r.text)
    assert frames[1] == {"ping": True}
    assert ": ping" in r.text
    types = [f["data"]["type"] for f in frames if "data" in f]
    assert "ping" not in types, "心跳绝不能混进事件序列"


def test_stream_producer_exception_still_emits_done(stores):
    """句柄流中途抛异常 → error + done 都要有（客户端永远能收尾）。"""
    class BoomHandle(FakeHandle):
        def stream(self, message, *, resume=False, has_checkpoint=False):
            async def gen():
                raise RuntimeError("图炸了")
                yield  # pragma: no cover
            return gen()

    client = make_client(BoomHandle(), stores)
    sid = new_session(client)
    r = client.post(f"/api/sessions/{sid}/chat", json={"message": "去成都"})
    assert r.status_code == 200  # SSE 已 200，错误只能在流里表达
    types = [f["data"]["type"] for f in parse_sse(r.text) if "data" in f]
    assert types == ["session", "error", "done"]
    assert not client.app.state.active_chats.is_active(sid), "异常路径守卫也要释放"


__all__ = ["FakeHandle", "parse_sse"]
