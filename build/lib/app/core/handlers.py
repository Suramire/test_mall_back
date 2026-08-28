"""全局异常处理器：统一转成 {code,message,data,traceId}，业务错误 HTTP 200。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core import errors
from app.core.tenant_context import TenantContextMissingError
from app.core.exceptions import BizError

logger = logging.getLogger(__name__)


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(BizError)
    async def _biz_error_handler(request: Request, exc: BizError):
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "code": exc.code,
                "message": exc.message,
                "data": exc.data,
                "traceId": _trace_id(request),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        fields: dict[str, str] = {}
        for e in exc.errors():
            loc = ".".join(str(x) for x in e["loc"] if x != "body")
            fields[loc] = e["msg"]
        return JSONResponse(
            status_code=200,
            content={
                "code": errors.BizCode.PARAM_ERROR,
                "message": "参数校验失败",
                "data": {"fields": fields},
                "traceId": _trace_id(request),
            },
        )

    @app.exception_handler(TenantContextMissingError)
    async def _tenant_ctx_handler(request: Request, exc: TenantContextMissingError):
        return JSONResponse(
            status_code=200,
            content={
                "code": errors.BizCode.UNAUTHORIZED,
                "message": str(exc),
                "data": None,
                "traceId": _trace_id(request),
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request: Request, exc: StarletteHTTPException):
        # API 契约规定业务失败也返回 HTTP 200，由 code 驱动前端提示。
        # 部分历史 handler 仍使用 HTTPException；在此统一收敛，避免 Axios
        # 直接进入网络异常分支、导致用户看不到可读的业务错误。
        # 仅 API 遵循该统一业务信封；根路径、文档及将来的静态资源仍应保持
        # 标准 HTTP 语义（例如静态文件 404 不能被伪装成 200）。
        if not request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
            )

        code_by_status = {
            400: errors.BizCode.PARAM_ERROR,
            401: errors.BizCode.UNAUTHORIZED,
            403: errors.BizCode.FORBIDDEN,
            404: errors.BizCode.NOT_FOUND,
            405: errors.BizCode.METHOD_NOT_ALLOWED,
            409: errors.BizCode.CONFLICT,
        }
        return JSONResponse(
            status_code=200,
            content={
                "code": code_by_status.get(exc.status_code, errors.BizCode.INTERNAL_ERROR),
                "message": str(exc.detail),
                "data": None,
                "traceId": _trace_id(request),
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception):
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "code": 50000,
                "message": "系统内部错误",
                "data": None,
                "traceId": _trace_id(request),
            },
        )
