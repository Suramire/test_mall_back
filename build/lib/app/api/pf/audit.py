"""平台端审计日志 /api/pf/audit（P1，列表查询）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.deps import require_perms
from app.core.response import page as page_response
from app.db.session import SessionLocal
from sqlalchemy import select

from app.models.pf_audit_log import PfAuditLog

router = APIRouter(prefix="/audit", tags=["平台-审计"])


@router.get("")
def list_audit(
    action: str | None = None,
    tenantId: int | None = None,
    operator: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _: None = Depends(require_perms("PF_AUDIT_LOG")),
):
    with SessionLocal() as session:
        stmt = select(PfAuditLog)
        if action:
            stmt = stmt.where(PfAuditLog.action == action)
        if tenantId is not None:
            stmt = stmt.where(PfAuditLog.tenant_id == tenantId)
        if operator:
            stmt = stmt.where(PfAuditLog.operator_name == operator)
        total = session.scalar(select(__import__("sqlalchemy").func.count()).select_from(stmt.subquery())) or 0
        rows = session.scalars(stmt.order_by(PfAuditLog.id.desc()).offset((page - 1) * size).limit(size)).all()
        items = [
            {
                "id": a.id, "operatorId": a.operator_id, "operatorName": a.operator_name,
                "scope": a.scope, "tenantId": a.tenant_id, "action": a.action,
                "targetType": a.target_type, "targetId": a.target_id, "detail": a.detail,
                "ip": a.ip, "createdAt": a.created_at.isoformat() if a.created_at else None,
            }
            for a in rows
        ]
        return page_response(items, total, page, size)
