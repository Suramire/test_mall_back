"""平台域模型：pf_feature（功能点字典，68 项）。"""
from __future__ import annotations

from sqlalchemy import BigInteger, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class PfFeature(Base, IdMixin, TimestampMixin):
    __tablename__ = "pf_feature"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(100), nullable=False, comment="{end}.{m1}.{m2}.{m3}")
    end_code: Mapped[str] = mapped_column(String(20), nullable=False, comment="user/merchant_pc/merchant_mp")
    l1_name: Mapped[str] = mapped_column(String(50), nullable=False, comment="一级模块")
    l2_name: Mapped[str] = mapped_column(String(50), nullable=False, comment="二级分组")
    l3_name: Mapped[str] = mapped_column(String(50), nullable=False, comment="三级功能")
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    default_on: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="开户默认勾选")
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        {"comment": "功能点字典(68项)"},
    )
