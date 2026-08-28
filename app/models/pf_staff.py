"""平台域模型：pf_staff（平台员工）。

平台员工表由平台统一维护，无 tenant_id 隔离字段，不调用 register_tenant_model。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin, SoftDeleteMixin


class PfStaff(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "pf_staff"

    account: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(100), nullable=False, comment="BCrypt cost=10")
    phone: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ENABLED")
    pwd_reset_required: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="连续失败次数,5次锁15min")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(3), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(3), nullable=True)
    last_login_ip: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    __table_args__ = (
        {"comment": "平台员工"},
    )
