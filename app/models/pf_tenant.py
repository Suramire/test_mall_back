"""平台域模型：pf_tenant（租户/商家）。"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BIGINT, Date, DateTime, String, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin, SoftDeleteMixin
from app.db.orm_hooks import register_tenant_model


class PfTenant(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "pf_tenant"

    tenant_no: Mapped[str] = mapped_column(String(20), nullable=False, comment="租户编号 M10001")
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="商家名称")
    contact_name: Mapped[str] = mapped_column(String(50), nullable=False, default="", comment="联系人")
    contact_phone: Mapped[str] = mapped_column(String(20), nullable=False, default="", comment="联系电话")
    qualification: Mapped[str] = mapped_column(String(255), nullable=False, default="", comment="资质/统一社会信用代码")
    # TenantStatus: NORMAL/TRIAL/EXPIRED/DISABLED
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="TRIAL")
    expire_at: Mapped[date | None] = mapped_column(Date, nullable=True, comment="服务到期日")
    goods_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="商品上限,0=不限")
    member_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="会员上限,0=不限")
    store_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="门店上限,0=不限")
    staff_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="员工上限,0=不限")
    # ★用户小程序AppID(商家自有主体)
    wx_appid: Mapped[str] = mapped_column(String(50), nullable=False, default="", comment="用户小程序AppID")
    wx_secret_enc: Mapped[str] = mapped_column(String(255), nullable=False, default="", comment="用户小程序Secret(AES)")
    wx_auth_status: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="0未授权 1已授权")
    # ★功能开通版本号,变更即令Token缓存失效
    perm_ver: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(3), nullable=True, comment="开通时间")

    __table_args__ = (
        {"comment": "租户(商家)表"},
    )


# pf_tenant 无 tenant_id 列：它是平台级表，不由 ORM 拦截注入。无需 register_tenant_model。
