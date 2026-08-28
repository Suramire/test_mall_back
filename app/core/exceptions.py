"""业务异常体系。统一由全局异常处理器捕获，返回 HTTP 200 + 业务 code。"""
from __future__ import annotations

from typing import Any


class BizError(Exception):
    """业务异常：HTTP 仍为 200，靠 code 区分。"""

    def __init__(self, code: int, message: str, data: Any = None, http_status: int = 200) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        self.http_status = http_status


class ParamError(BizError):
    """参数校验失败，data.fields 携带字段错误。"""

    def __init__(self, fields: dict[str, str] | None = None, message: str = "参数校验失败") -> None:
        super().__init__(code=40001, message=message, data={"fields": fields or {}})


class NotFoundError(BizError):
    def __init__(self, message: str = "资源不存在") -> None:
        super().__init__(code=40400, message=message)


class ConflictError(BizError):
    def __init__(self, message: str = "资源冲突") -> None:
        super().__init__(code=40900, message=message)


class UnauthorizedError(BizError):
    def __init__(self, message: str = "未登录或 Token 无效") -> None:
        super().__init__(code=40100, message=message)


class TokenExpiredError(BizError):
    def __init__(self, message: str = "Token 已过期") -> None:
        super().__init__(code=40101, message=message)


class ForbiddenError(BizError):
    def __init__(self, message: str = "无权执行此操作") -> None:
        super().__init__(code=40301, message=message)


class FeatureNotOpenError(BizError):
    def __init__(self, message: str = "该功能尚未开通") -> None:
        super().__init__(code=40302, message=message)


class TenantExpiredError(BizError):
    def __init__(self, message: str = "租户服务已到期") -> None:
        super().__init__(code=41001, message=message)


class TenantDisabledError(BizError):
    def __init__(self, message: str = "租户已被禁用") -> None:
        super().__init__(code=41002, message=message)
