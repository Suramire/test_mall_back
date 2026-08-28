"""平台域模型：pf_audit_log（审计日志，合规硬需求）。

tenant_id 语义为「对象归属」非隔离字段；平台统一读写，不 register_tenant_model。
"""
from __future__ import annotations

from sqlalchemy import BigInteger, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class PfAuditLog(Base, IdMixin, TimestampMixin):
    __tablename__ = "pf_audit_log"

    operator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operator_name: Mapped[str] = mapped_column(String(50), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False, comment="platform/merchant")
    tenant_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="涉及租户(代客必填)")
    impersonator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="代客态平台员工ID")
    action: Mapped[str] = mapped_column(String(100), nullable=False, comment="TENANT_IMPERSONATE/...")
    target_type: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    target_id: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="{before:{},after:{}}")
    ip: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    ua: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    trace_id: Mapped[str] = mapped_column(String(40), nullable=False, default="")

    __table_args__ = (
        {"comment": "审计日志"},
    )
