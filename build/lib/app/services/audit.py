"""审计日志写入（合规硬需求）。

在 service 层调用，写入 pf_audit_log。tenant_id 为对象归属语义，代客态填 impersonator_id。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.tenant_context import (
    get_impersonator_id,
    get_staff_id,
    get_staff_name,
)
from app.models.pf_audit_log import PfAuditLog


def _resolve_operator_name(session: Session, operator_id: int) -> str | None:
    """上下文未携带员工名时（中间件仅注入 sub），按 ID 回查平台员工姓名。"""
    if not operator_id:
        return None
    try:
        from app.models.pf_staff import PfStaff

        staff = session.get(PfStaff, operator_id)
        if staff is not None and staff.name:
            return staff.name
        from app.models.mc_staff import McStaff

        staff = session.get(McStaff, operator_id)
        if staff is not None and staff.name:
            return staff.name
    except Exception:
        pass
    return None


def write_audit(
    session: Session,
    *,
    action: str,
    target_type: str = "",
    target_id: str = "",
    detail: dict[str, Any] | None = None,
    scope: str = "platform",
    tenant_id: int | None = None,
    ip: str = "",
    trace_id: str = "",
) -> None:
    """写入一条审计记录。operator 取当前平台员工上下文。"""
    operator_id = get_staff_id() or 0
    operator_name = get_staff_name() or _resolve_operator_name(session, operator_id) or "system"
    imp_id = get_impersonator_id()
    session.add(PfAuditLog(
        operator_id=operator_id,
        operator_name=operator_name,
        scope=scope,
        tenant_id=tenant_id,
        impersonator_id=imp_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        detail=detail,
        ip=ip,
        ua="",
        trace_id=trace_id,
    ))
