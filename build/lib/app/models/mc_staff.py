"""商家域模型：mc_staff（商家员工/管理员，BCrypt）。

业务表：含 tenant_id，由 ORM 拦截强制注入（register_tenant_model）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin, SoftDeleteMixin
from app.db.orm_hooks import register_tenant_model


class McStaff(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "mc_staff"

    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(100), nullable=False, comment="BCrypt cost=10")
    phone: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, comment="商家角色(0=内置管理员)")
    wecom_userid: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="企业微信用户ID，租户内唯一")
    store_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="员工归属门店，NULL=全店/管理员")
    is_admin: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="1=租户管理员")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ENABLED")
    pwd_reset_required: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(3), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(3), nullable=True)
    last_login_ip: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    __table_args__ = (
        {"comment": "商家员工"},
    )


register_tenant_model(McStaff)
