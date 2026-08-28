"""平台端员工 /api/pf/staff。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select

from app.core.deps import require_perms
from app.core.exceptions import NotFoundError
from app.core.response import ok
from app.core.response import page as page_response
from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.pf_role import PfRole
from app.models.pf_staff import PfStaff
from app.schemas import StaffItem, StaffReq
from app.services.audit import write_audit

router = APIRouter(prefix="/staff", tags=["平台-员工"])


def _mask_phone(phone: str) -> str:
    """手机号脱敏：138****8000。不足 11 位原样返回。"""
    if phone and len(phone) == 11:
        return phone[:3] + "****" + phone[7:]
    return phone


def _to_item(s: PfStaff, role_name: str = "") -> dict:
    return StaffItem(
        id=s.id, account=s.account, name=s.name, phone=_mask_phone(s.phone),
        roleId=s.role_id, roleName=role_name, status=s.status, lastLoginAt=s.last_login_at,
    ).model_dump()


@router.get("")
def list_staff(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _: None = Depends(require_perms("PF_STAFF")),
):
    with SessionLocal() as session:
        base = select(PfStaff).where(PfStaff.deleted_at.is_(None))
        total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
        rows = session.scalars(
            base.order_by(PfStaff.id).offset((page - 1) * size).limit(size)
        ).all()
        roles = {r.id: r.name for r in session.scalars(select(PfRole)).all()}
        items = [_to_item(s, roles.get(s.role_id, "")) for s in rows]
        return page_response(items, total, page, size)


@router.post("")
def create_staff(req: StaffReq, request: Request, _: None = Depends(require_perms("PF_STAFF"))):
    with SessionLocal() as session:
        if session.scalar(select(PfStaff).where(PfStaff.account == req.account, PfStaff.deleted_at.is_(None))):
            raise HTTPException(409, "员工账号已存在")
        if not session.get(PfRole, req.roleId):
            raise NotFoundError("角色不存在")
        session.add(PfStaff(
            account=req.account, name=req.name, password_hash=hash_password(req.password),
            phone=req.phone, role_id=req.roleId, status="ENABLED",
        ))
        session.flush()
        new_id = session.query(PfStaff).filter_by(account=req.account).order_by(PfStaff.id.desc()).first().id
        write_audit(
            session, action="STAFF_CREATE", target_type="pf_staff", target_id=str(new_id),
            detail={"after": {"account": req.account, "name": req.name, "role_id": req.roleId}},
            ip=request.client.host if request.client else "",
        )
        session.commit()
        return ok({"id": new_id})


@router.put("/{staff_id}")
def update_staff(staff_id: int, req: StaffReq, request: Request, _: None = Depends(require_perms("PF_STAFF"))):
    with SessionLocal() as session:
        s = session.get(PfStaff, staff_id)
        if not s or s.deleted_at is not None:
            raise NotFoundError("员工不存在")
        before = {"name": s.name, "phone": _mask_phone(s.phone), "role_id": s.role_id}
        s.name = req.name
        s.phone = req.phone
        s.role_id = req.roleId
        pwd_changed = bool(req.password)
        if pwd_changed:
            s.password_hash = hash_password(req.password)
            s.pwd_reset_required = 1
        write_audit(
            session, action="STAFF_UPDATE", target_type="pf_staff", target_id=str(staff_id),
            detail={"before": before, "after": {"name": req.name, "phone": _mask_phone(req.phone), "role_id": req.roleId, "pwd_changed": pwd_changed}},
            ip=request.client.host if request.client else "",
        )
        session.commit()
        return ok()


@router.post("/{staff_id}/reset-pwd")
def reset_pwd(staff_id: int, request: Request, _: None = Depends(require_perms("PF_STAFF_RESET_PWD"))):
    import secrets
    import string
    with SessionLocal() as session:
        s = session.get(PfStaff, staff_id)
        if not s or s.deleted_at is not None:
            raise NotFoundError("员工不存在")
        alphabet = string.ascii_letters + string.digits
        new_pwd = "".join(secrets.choice(alphabet) for _ in range(10))
        s.password_hash = hash_password(new_pwd)
        s.pwd_reset_required = 1
        write_audit(
            session, action="STAFF_RESET_PWD", target_type="pf_staff", target_id=str(staff_id),
            detail={"after": {"account": s.account, "pwd_reset_required": 1}},
            ip=request.client.host if request.client else "",
        )
        session.commit()
    # 重置密码：自增该员工 token 版本，使其旧 token 立即失效
    from app.core.redis import bump_token_version
    from app.core.security import SCOPE_PLATFORM
    bump_token_version(SCOPE_PLATFORM, str(staff_id))
    return ok({"newPassword": new_pwd})


@router.post("/{staff_id}/toggle-status")
def toggle_status(staff_id: int, request: Request, _: None = Depends(require_perms("PF_STAFF"))):
    with SessionLocal() as session:
        s = session.get(PfStaff, staff_id)
        if not s or s.deleted_at is not None:
            raise NotFoundError("员工不存在")
        before = s.status
        s.status = "DISABLED" if s.status == "ENABLED" else "ENABLED"
        write_audit(
            session, action="STAFF_STATUS_CHANGE", target_type="pf_staff", target_id=str(staff_id),
            detail={"before": {"status": before}, "after": {"status": s.status}},
            ip=request.client.host if request.client else "",
        )
        session.commit()
        return ok({"status": s.status})
