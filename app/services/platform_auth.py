"""平台员工认证服务：登录/刷新/登出/改密。"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.redis import blacklist_token, bump_token_version
from app.core.security import (
    SCOPE_PLATFORM,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    token_version_valid,
    verify_password,
)
from app.models.pf_role import PfRole
from app.models.pf_staff import PfStaff


def _issue_tokens(staff: PfStaff, role: PfRole) -> dict[str, Any]:
    access = create_access_token(
        subject=str(staff.id),
        scope=SCOPE_PLATFORM,
        perms=list(role.perms or []),
    )
    refresh = create_refresh_token(subject=str(staff.id), scope=SCOPE_PLATFORM)
    return {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresIn": 7200,
        "user": {
            "id": staff.id,
            "name": staff.name,
            "roleName": role.name,
            "perms": list(role.perms or []),
            "pwdResetRequired": bool(staff.pwd_reset_required),
        },
    }


def login(session: Session, account: str, password: str, ip: str = "") -> dict:
    staff = session.scalar(select(PfStaff).where(PfStaff.account == account, PfStaff.deleted_at.is_(None)))
    if not staff or not verify_password(password, staff.password_hash):
        raise UnauthorizedError("账号或密码错误")
    if staff.status != "ENABLED":
        raise ForbiddenError("账号已被禁用")
    role = session.get(PfRole, staff.role_id)
    if not role:
        raise ForbiddenError("角色不存在")
    staff.last_login_at = datetime.now(UTC).replace(tzinfo=None)
    staff.last_login_ip = ip
    staff.fail_count = 0
    return _issue_tokens(staff, role)


def refresh(session: Session, refresh_token: str) -> dict:
    payload = decode_token(refresh_token)
    if payload.get("scope") != SCOPE_PLATFORM or payload.get("typ") != "refresh":
        raise UnauthorizedError("刷新令牌无效")
    # 密码重置后旧 token 吊销：刷新同样受版本约束
    if not token_version_valid(payload):
        raise UnauthorizedError("登录态已失效，请重新登录")
    staff = session.get(PfStaff, int(payload["sub"]))
    if not staff or staff.status != "ENABLED":
        raise UnauthorizedError("账号不可用")
    role = session.get(PfRole, staff.role_id)
    return _issue_tokens(staff, role)


def logout(access_token: str) -> None:
    try:
        payload = decode_token(access_token, verify_exp=False)
        jti = payload.get("jti")
        if jti:
            blacklist_token(jti)
    except Exception:
        pass


def change_password(session: Session, staff_id: int, old_pwd: str, new_pwd: str) -> None:
    staff = session.get(PfStaff, staff_id)
    if not staff or not verify_password(old_pwd, staff.password_hash):
        raise UnauthorizedError("原密码错误")
    staff.password_hash = hash_password(new_pwd)
    staff.pwd_reset_required = 0
    # 密码变更：自增 token 版本，使该员工的旧 access/refresh token 立即失效，
    # 无需维护 jti 黑名单列表，前端改密后下次请求即被拒绝并重新登录。
    bump_token_version(SCOPE_PLATFORM, str(staff_id))
