"""FastAPI 依赖：解析当前登录主体与权限守卫。

依赖 TenantGuard 中间件已写入 request.state.auth（JWT payload）。
平台端 scope=platform；商家端 scope=merchant；用户端 scope=customer。
"""
from __future__ import annotations

from functools import wraps

from fastapi import Request

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import SCOPE_PLATFORM, SCOPE_MERCHANT


def get_auth_payload(request: Request) -> dict:
    payload = getattr(request.state, "auth", None)
    if not payload:
        raise UnauthorizedError("未登录或 Token 无效")
    return payload


def get_current_platform_staff(request: Request) -> dict:
    """要求平台端登录，返回 payload（含 sub/perms）。"""
    payload = get_auth_payload(request)
    if payload.get("scope") != SCOPE_PLATFORM:
        raise ForbiddenError("仅平台端可访问")
    return payload


def require_merchant(request: Request) -> dict:
    """要求商家端登录，返回 payload（含 tid/sub）。

    统一商家端 scope 守卫，替代业务层散落的 `merchant_ctx()` 式手写校验。
    注意：仅校验 scope，不阻止顾客端用 /c 前缀绕过——前缀隔离见 router.py。
    """
    payload = get_auth_payload(request)
    if payload.get("scope") != SCOPE_MERCHANT:
        raise ForbiddenError("仅商家端可访问")
    return payload


def require_perms(*codes: str):
    """装饰器式依赖工厂：要求 JWT perms 含任一所需码。

    用法：Depends(require_perms("PF_MERCHANT_EDIT"))
    """

    def _dep(request: Request) -> None:
        payload = get_auth_payload(request)
        perms: list = payload.get("perms") or []
        if not codes:
            return
        if not any(c in perms for c in codes):
            raise ForbiddenError("无权限执行此操作")

    return _dep


def current_staff_id(request: Request) -> int:
    payload = get_auth_payload(request)
    return int(payload["sub"])
