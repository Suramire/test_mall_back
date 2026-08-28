"""平台端会员敏感信息查看 /api/pf/member。

平台跨租户查询会员，涉及业务表必须 skip_tenant_filter 逃逸租户钩子。
明文手机号仅允许通过 reveal-phone 显式获取，且强制写入 MEMBER_PHONE_VIEW 审计。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from sqlalchemy import select

from app.core.deps import require_perms
from app.core.exceptions import NotFoundError
from app.core.response import ok
from app.db.session import SessionLocal
from app.models.mb_member import MbMember
from app.services.audit import write_audit

router = APIRouter(prefix="/member", tags=["平台-会员"])


def _get_member_cross_tenant(session, member_id: int) -> MbMember:
    m = session.scalars(
        select(MbMember).where(MbMember.id == member_id, MbMember.deleted_at.is_(None))
        .execution_options(skip_tenant_filter=True)
    ).first()
    if not m:
        raise NotFoundError("会员不存在")
    return m


@router.get("/{member_id}")
def member_detail(member_id: int, _: None = Depends(require_perms("PF_MEMBER_VIEW"))):
    """平台查看会员详情（手机号仅脱敏态，不触发敏感审计）。"""
    with SessionLocal() as session:
        m = _get_member_cross_tenant(session, member_id)
        return ok({
            "id": m.id,
            "tenantId": m.tenant_id,
            "memberNo": m.member_no,
            "nickname": m.nickname,
            "phoneMask": m.phone_mask,
            "pointsBalance": m.points_balance,
            "totalAmount": str(m.total_amount),
            "totalOrderCount": m.total_order_count,
            "levelId": m.level_id,
        })


@router.post("/{member_id}/reveal-phone")
def reveal_phone(member_id: int, request: Request, _: None = Depends(require_perms("PF_MEMBER_VIEW"))):
    """平台查看会员明文手机号：返回密文列存储值并写入 MEMBER_PHONE_VIEW 审计。"""
    with SessionLocal() as session:
        m = _get_member_cross_tenant(session, member_id)
        data = {
            "id": m.id,
            "tenantId": m.tenant_id,
            "memberNo": m.member_no,
            "phone": m.phone_enc,
        }
        write_audit(
            session,
            action="MEMBER_PHONE_VIEW",
            target_type="mb_member",
            target_id=str(member_id),
            scope="platform",
            tenant_id=data["tenantId"],
            detail={"after": {"memberNo": data["memberNo"]}},
            ip=request.client.host if request.client else "",
        )
        session.commit()
        return ok(data)
