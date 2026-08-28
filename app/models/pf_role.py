"""平台域模型：pf_role（平台角色，含权限码 JSON）。"""
from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin, SoftDeleteMixin


class PfRole(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "pf_role"

    name: Mapped[str] = mapped_column(String(50), nullable=False)
    remark: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    perms: Mapped[list] = mapped_column(JSON, nullable=False, comment='权限码数组 ["PF_DASHBOARD",...]')
    is_system: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="系统预置不可删")

    __table_args__ = (
        {"comment": "平台角色"},
    )
