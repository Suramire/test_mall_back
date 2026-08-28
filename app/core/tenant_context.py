"""多租户上下文。

tenant_id 永不从前端接收；由 TenantGuard 中间件从 JWT 解析后 set 到 ContextVar。
平台端 (/api/pf) 不设置 tenant_id；其余三端缺失则 Fail-Fast。
"""
from __future__ import annotations

from contextvars import ContextVar, Token

from app.core.exceptions import UnauthorizedError

# 当前租户上下文
_current_tenant_id: ContextVar[int | None] = ContextVar("current_tenant_id", default=None)
# 当前员工上下文
_current_staff_id: ContextVar[int | None] = ContextVar("current_staff_id", default=None)
# 当前员工名
_current_staff_name: ContextVar[str | None] = ContextVar("current_staff_name", default=None)
# 是否代客态
_current_impersonating: ContextVar[bool] = ContextVar("current_impersonating", default=False)
# 代客态下的平台员工ID
_current_impersonator_id: ContextVar[int | None] = ContextVar("current_impersonator_id", default=None)


class TenantContextMissingError(Exception):
    """访问 tenant_id 但上下文缺失时抛出（Fail-Fast）。"""

    pass


def set_tenant(tenant_id: int | None) -> Token:
    return _current_tenant_id.set(tenant_id)


def set_staff(staff_id: int | None, name: str | None = None) -> Token:
    tokens = (_current_staff_id.set(staff_id), _current_staff_name.set(name))
    # 返回组合 token 用 reset 不方便，直接返回 staff token 由调用方管理
    return tokens[0]


def set_impersonation(impersonating: bool, impersonator_id: int | None = None) -> Token:
    tokens = (_current_impersonating.set(impersonating), _current_impersonator_id.set(impersonator_id))
    return tokens[0]


def get_tenant_id() -> int | None:
    return _current_tenant_id.get()


def require_tenant_id() -> int:
    """取当前租户，缺失则 Fail-Fast。业务表查询必须调用此方法。"""
    tid = _current_tenant_id.get()
    if tid is None:
        raise TenantContextMissingError("当前请求缺少租户上下文(tenant_id)")
    return tid


def get_staff_id() -> int | None:
    return _current_staff_id.get()


def get_staff_name() -> str | None:
    return _current_staff_name.get()


def is_impersonating() -> bool:
    return _current_impersonating.get()


def get_impersonator_id() -> int | None:
    return _current_impersonator_id.get()


def reset() -> None:
    """请求结束后清理上下文，避免串租户。"""
    _current_tenant_id.set(None)
    _current_staff_id.set(None)
    _current_staff_name.set(None)
    _current_impersonating.set(False)
    _current_impersonator_id.set(None)
