"""平台端认证 /api/pf/auth。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.core.deps import current_staff_id, get_current_platform_staff
from app.core.errors import BizCode
from app.core.response import err, ok
from app.db.session import SessionLocal
from app.schemas import ChangePasswordReq, PlatformLoginReq
from app.services import platform_auth

router = APIRouter(prefix="/auth", tags=["平台-认证"])


@router.post("/login")
def login(req: PlatformLoginReq, request: Request):
    with SessionLocal() as session:
        tokens = platform_auth.login(session, req.account, req.password, ip=request.client.host if request.client else "")
        session.commit()
        return ok(tokens)


@router.post("/refresh")
def refresh(request: Request):
    # refresh 使用 refresh token（Authorization: Bearer <refresh>），不要求 access 有效
    auth = request.headers.get("Authorization", "")[7:].strip()
    with SessionLocal() as session:
        tokens = platform_auth.refresh(session, auth)
        return ok(tokens)


@router.post("/logout")
def logout(request: Request):
    auth = request.headers.get("Authorization", "")[7:].strip()
    platform_auth.logout(auth)
    return ok()


@router.get("/me")
def me(request: Request):
    sid = current_staff_id(request)
    with SessionLocal() as session:
        from app.models.pf_staff import PfStaff

        s = session.get(PfStaff, sid)
        if not s or s.deleted_at is not None:
            from app.core.exceptions import NotFoundError

            raise NotFoundError("员工不存在")
        payload = get_current_platform_staff(request)
        return ok({
            "id": s.id,
            "name": s.name,
            "account": s.account,
            "roleId": s.role_id,
            "scope": payload.get("scope"),
            "perms": payload.get("perms") or [],
        })


@router.put("/password")
def change_password(req: ChangePasswordReq, request: Request):
    if len(req.newPassword) < 6:
        return err(BizCode.PARAM_ERROR, '新密码至少 6 位')
    if req.confirmPassword is not None and req.confirmPassword != req.newPassword:
        return err(BizCode.PARAM_ERROR, '两次输入的新密码不一致')
    sid = current_staff_id(request)
    with SessionLocal() as session:
        platform_auth.change_password(session, sid, req.oldPassword, req.newPassword)
        from app.services.audit import write_audit

        write_audit(
            session, action="PASSWORD_SELF_CHANGE", target_type="pf_staff", target_id=str(sid),
            ip=request.client.host if request.client else "",
        )
        session.commit()
        return ok(message="密码已修改，请重新登录")
