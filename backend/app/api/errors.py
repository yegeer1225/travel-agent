"""统一异常与错误响应（`docs/api.md` 1.2 / 六 的落地）。

═══════════════════════════════════════════════════════════════
 为什么必须在这里"劫持" FastAPI 的默认错误
═══════════════════════════════════════════════════════════════

FastAPI/Starlette 默认的错误长得像 `{"detail": "Not Found"}`，
而契约规定**所有非 2xx 都是同一个形状**（`ErrorBody`）。
不注册 handler 改写的话，前端会同时面对两种错误格式 ——
正常错误走 `error.code`，漏网的走 `detail`，前端按 code 分支的逻辑
对后者全部失效。这是 api.md 六里点名"最容易漏的一处不一致"。

三劫：`AppError`（我们主动抛的）、`RequestValidationError`
（Pydantic 在门口拦下的非法请求体）、兜底 `Exception`（500）。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas import ErrorBody, ErrorDetail


class AppError(Exception):
    """业务异常。**code 是给机器判别的契约**，msg 是给人看的中文（会改，别拿它分支）。

    用法（api.md 六照抄）：
        raise AppError("not_found", "会话不存在", 404)
        raise AppError("invalid_param", "arrive 必须是 HH:MM", 400)
    """

    def __init__(self, code: str, msg: str, status: int = 400, detail: dict | None = None) -> None:
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.status = status
        self.detail = detail

    def to_body(self) -> ErrorBody:
        return ErrorBody(error=ErrorDetail(code=self.code, msg=self.msg, detail=self.detail))


class RateLimited(AppError):
    """限流命中（D28）。→ `429` + `Retry-After` 头 + `detail.retry_after`。

    ⚠️ `detail.retry_after` 是前端倒计时的**主路径**（fetch 读
    `Retry-After` 头要 CORS expose，不可靠）；头是给网关/浏览器看的备份。
    """

    def __init__(self, msg: str, retry_after: int) -> None:
        super().__init__(
            code="rate_limited",
            msg=msg,
            status=429,
            detail={"retry_after": max(1, int(retry_after))},
        )
        self.retry_after = max(1, int(retry_after))


def register_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, RateLimited) else None
        return JSONResponse(status_code=exc.status, content=exc.to_body().model_dump(), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 契约把"参数不合法"收敛成 invalid_param。msg 取第一条给用户，
        # 完整明细放 detail（前端一般不展示，但排查时有用）。
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        body = ErrorBody(
            error=ErrorDetail(
                code="invalid_param",
                msg=f"{loc}：{first.get('msg', '参数不合法')}",
                detail={"errors": exc.errors()},
            )
        )
        return JSONResponse(status_code=400, content=body.model_dump())

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """路由不存在 / 方法不对等 Starlette 自己抛的，也改写成 ErrorBody。

        code 从状态码映射到契约错误码表里最接近的一个 ——
        404 就是 not_found（语义一致：不存在 = 无权访问，都叫 not_found）。
        """
        code_by_status = {404: "not_found", 405: "invalid_param", 401: "unauthorized", 403: "not_found"}
        body = ErrorBody(
            error=ErrorDetail(
                code=code_by_status.get(exc.status_code, "internal_error" if exc.status_code >= 500 else "invalid_param"),
                msg=str(exc.detail),
            )
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """兜底 500。⚠️ **不把异常文本回给客户端** —— 那可能带路径/DSN/SQL，
        泄露内部结构。详情在服务端日志里看。"""
        import logging

        logging.getLogger("app.api").exception("未预期异常：%s", exc)
        body = ErrorBody(error=ErrorDetail(code="internal_error", msg="服务出错了，请稍后重试"))
        return JSONResponse(status_code=500, content=body.model_dump())


__all__ = ["AppError", "RateLimited", "register_handlers"]
