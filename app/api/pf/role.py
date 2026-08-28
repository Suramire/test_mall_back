"""平台端角色 /api/pf/role。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import require_perms
from app.core.exceptions import ForbiddenError
from app.core.response import ok
from app.db.session import SessionLocal
from app.models.pf_role import PfRole
from app.schemas import RoleItem, RoleReq
from app.services.audit import write_audit

router = APIRouter(prefix="/role", tags=["平台-角色"])
_PLATFORM_PERMS = frozenset({
    "PF_DASHBOARD", "PF_MERCHANT_LIST", "PF_MERCHANT_EDIT", "PF_MERCHANT_STATUS",
    "PF_MERCHANT_IMPERSONATE", "PF_MERCHANT_RESET_PWD", "PF_FEATURE_EDIT", "PF_ROLE",
    "PF_STAFF", "PF_STAFF_RESET_PWD", "PF_MSG_TEMPLATE", "PF_AUDIT_LOG", "PF_MEMBER_VIEW",
})

def _check_perms(perms: list[str] | None) -> None:
    unknown = sorted(set(perms or []) - _PLATFORM_PERMS)
    if unknown:
        raise HTTPException(400, f"存在未配置的平台权限: {','.join(unknown)}")


def _to_item(r: PfRole) -> dict:
    return RoleItem(id=r.id, name=r.name, remark=r.remark, perms=list(r.perms or []), isSystem=r.is_system).model_dump()


@router.get("")
def list_roles(_: None = Depends(require_perms("PF_ROLE"))):
    with SessionLocal() as session:
        rows = session.scalars(select(PfRole).where(PfRole.deleted_at.is_(None)).order_by(PfRole.id)).all()
        return ok([_to_item(r) for r in rows])


@router.get("/perm-tree")
def perm_tree(_: None = Depends(require_perms("PF_ROLE"))):
    # 平台权限码字典（与 seed PLATFORM_ROLE_TEMPLATES.perms 对齐）
    perms = [
        "PF_DASHBOARD", "PF_MERCHANT_LIST", "PF_MERCHANT_EDIT", "PF_MERCHANT_STATUS",
        "PF_MERCHANT_IMPERSONATE", "PF_MERCHANT_RESET_PWD", "PF_FEATURE_EDIT",
        "PF_ROLE", "PF_STAFF", "PF_STAFF_RESET_PWD", "PF_MSG_TEMPLATE", "PF_AUDIT_LOG",
        "PF_MEMBER_VIEW",
    ]
    return ok(perms)

@router.get("/{role_id}/perms")
def role_perms(role_id: int, _: None = Depends(require_perms("PF_ROLE"))):
    """返回指定角色权限码，供后台查看权限详情。"""
    with SessionLocal() as session:
        r = session.get(PfRole, role_id)
        if not r or r.deleted_at is not None:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("角色不存在")
        return ok(list(r.perms or []))

@router.get("/{role_id}")
def role_detail(role_id: int, _: None = Depends(require_perms("PF_ROLE"))):
    """角色详情，返回与列表一致的权限快照，供编辑页回填。"""
    with SessionLocal() as session:
        r = session.get(PfRole, role_id)
        if not r or r.deleted_at is not None:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("角色不存在")
        return ok(_to_item(r))

@router.put("/{role_id}/perms")
def set_role_perms(role_id: int, perms: list[str], _: None = Depends(require_perms("PF_ROLE"))):
    _check_perms(perms)
    with SessionLocal() as session:
        r=session.get(PfRole, role_id)
        if not r or r.deleted_at is not None:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("角色不存在")
        if r.is_system:
            from app.core.exceptions import ForbiddenError
            raise ForbiddenError("系统预置角色不可编辑")
        before = list(r.perms or [])
        r.perms=perms
        write_audit(
            session, action="ROLE_PERMS_CHANGE", target_type="pf_role", target_id=str(role_id),
            detail={"before": {"perms": before}, "after": {"perms": list(perms)}},
        )
        session.commit(); return ok()


@router.post("")
def create_role(req: RoleReq, _: None = Depends(require_perms("PF_ROLE"))):
    _check_perms(req.perms)
    with SessionLocal() as session:
        if session.scalar(select(PfRole).where(PfRole.name == req.name, PfRole.deleted_at.is_(None))):
            raise HTTPException(409, "角色名称已存在")
        role=PfRole(name=req.name, remark=req.remark, perms=req.perms, is_system=0)
        session.add(role)
        session.flush()
        write_audit(
            session, action="ROLE_CREATE", target_type="pf_role", target_id=str(role.id),
            detail={"after": {"name": req.name, "perms": list(req.perms or [])}},
        )
        session.commit()
        return ok({"id": role.id, "name": role.name, "remark": role.remark, "perms": list(role.perms or [])})


@router.put("/{role_id}")
def update_role(role_id: int, req: RoleReq, _: None = Depends(require_perms("PF_ROLE"))):
    _check_perms(req.perms)
    with SessionLocal() as session:
        r = session.get(PfRole, role_id)
        if not r or r.deleted_at is not None:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("角色不存在")
        if r.is_system:
            raise ForbiddenError("系统预置角色不可编辑")
        before = {"name": r.name, "remark": r.remark, "perms": list(r.perms or [])}
        r.name = req.name
        r.remark = req.remark
        r.perms = req.perms
        write_audit(
            session, action="ROLE_UPDATE", target_type="pf_role", target_id=str(role_id),
            detail={"before": before, "after": {"name": req.name, "remark": req.remark, "perms": list(req.perms or [])}},
        )
        session.commit()
        return ok()


@router.delete("/{role_id}")
def delete_role(role_id: int, _: None = Depends(require_perms("PF_ROLE"))):
    with SessionLocal() as session:
        r = session.get(PfRole, role_id)
        if not r or r.deleted_at is not None:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("角色不存在")
        if r.is_system:
            raise ForbiddenError("系统预置角色不可删除")
        r.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        write_audit(
            session, action="ROLE_DELETE", target_type="pf_role", target_id=str(role_id),
            detail={"before": {"name": r.name}},
        )
        session.commit()
        return ok()
