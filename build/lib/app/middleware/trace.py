"""TraceId 中间件：生成/传播 X-Trace-Id，写入 request.state、响应头和统一响应体。"""
from __future__ import annotations

import json
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id")
        if not trace_id:
            trace_id = uuid.uuid4().hex
        request.state.trace_id = trace_id

        response: Response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return await self._with_trace_id(response, trace_id)

    @staticmethod
    async def _with_trace_id(response: Response, trace_id: str) -> Response:
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        try:
            payload = json.loads(body.decode() or "null")
        except (UnicodeDecodeError, json.JSONDecodeError):
            new_response = Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
                background=response.background,
            )
            new_response.headers["X-Trace-Id"] = trace_id
            return new_response

        if isinstance(payload, dict) and {"code", "message", "data"} <= payload.keys():
            payload["traceId"] = payload.get("traceId") or trace_id
            body = json.dumps(payload, ensure_ascii=False).encode()

        headers = dict(response.headers)
        headers.pop("content-length", None)
        new_response = Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
            background=response.background,
        )
        new_response.headers["X-Trace-Id"] = trace_id
        return new_response
