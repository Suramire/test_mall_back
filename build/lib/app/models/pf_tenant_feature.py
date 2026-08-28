"""平台域模型：pf_tenant_feature（租户功能开通）。

注意：本表虽名为 *tenant_feature*，但 tenant_id 是「对象归属」而非隔离字段；
平台端统一读写，不由 ORM 拦截注入。故不调用 register_tenant_model。
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class PfTenantFeature(Base, IdMixin, TimestampMixin):
    __tablename__ = "pf_tenant_feature"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    feature_code: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        {"comment": "租户功能开通表"},
    )
