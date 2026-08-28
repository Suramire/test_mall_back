"""UTC 时间戳输出中间件：JSON 响应中的 ISO 时间统一为带 Z 后缀的 UTC。

口径：库内 naive DATETIME 一律视为 UTC（见 docs/utc-migration.md）。
naive 串补 "Z"；已带偏移（如 +00:00/+08:00）的统一归一为 "Z"，
保证全站 API 输出 ISO-8601 UTC 单一格式。
"""
from __future__ import annotations

import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)(?:Z|[+-]\d{2}:?\d{2})?")


class UtcTimestampMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        try:
            body = _TS_RE.sub(lambda m: m.group(1) + "Z", body.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            pass

        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
            background=response.background,
        )
