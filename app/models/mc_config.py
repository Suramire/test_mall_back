"""商家配置域模型：mc_role / mc_shop / mc_store / mc_pay_config / mc_msg_config / mc_notice。

均为业务表（含 tenant_id），由 ORM 拦截强制注入。DDL 口径见 docs/architecture/02-数据库设计.md §2。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BIGINT_U, DT3, TINYINT, Base, IdMixin, SoftDeleteMixin, TimestampMixin
from app.db.orm_hooks import register_tenant_model


class McRole(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """商家角色（店长/收银员/客服 预置）。"""

    __tablename__ = "mc_role"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    remark: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    perms: Mapped[list[str]] = mapped_column(JSON, nullable=False, comment="商家34项权限码子集")
    is_system: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="店长/收银员/客服 预置"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "deleted_at", name="uk_tenant_name"),
        {"comment": "商家角色"},
    )


class McShop(Base, IdMixin, TimestampMixin):
    """店铺信息（每租户一行）。无软删。"""

    __tablename__ = "mc_shop"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    logo: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    notice: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", comment="首页公告跑马灯"
    )
    intro: Mapped[str | None] = mapped_column(Text, nullable=True)
    share_title: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    share_cover: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    banners: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True, comment="[{img,channel,goodsId,sort}]"
    )
    service_qr: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", comment="企微客服二维码"
    )
    service_tel: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    service_time: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("tenant_id", name="uk_tenant"),
        {"comment": "店铺信息(每租户一行)"},
    )


class McStore(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """门店。"""

    __tablename__ = "mc_store"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    lng: Mapped[Decimal | None] = mapped_column(Numeric(10, 7), nullable=True)
    lat: Mapped[Decimal | None] = mapped_column(Numeric(10, 7), nullable=True)
    phone: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    business_hours: Mapped[str] = mapped_column(
        String(100), nullable=False, default="", comment="09:00-21:00"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ENABLED", comment="营业/停业"
    )
    is_pickup: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    is_verify: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_tenant_status_sort", "tenant_id", "status", "sort"),
        {"comment": "门店"},
    )


class McPayConfig(Base, IdMixin, TimestampMixin):
    """支付配置（每租户一行，支持 DIRECT 直连 / PARTNER 服务商 双模式）。"""

    __tablename__ = "mc_pay_config"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    pay_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="DIRECT",
        comment="支付模式: DIRECT=直连商户, PARTNER=服务商模式",
    )
    # 直连商户字段（PARTNER 模式下三证字段存【服务商】密钥）
    wx_mch_id: Mapped[str] = mapped_column(
        String(50), nullable=False, default="", comment="直连商户号; PARTNER 模式可空"
    )
    wx_api_key_enc: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        default="",
        comment="APIv3密钥 AES加密; PARTNER 模式存【服务商】密钥",
    )
    wx_cert_serial: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="",
        comment="商户证书序列号; PARTNER 模式存【服务商】序列号",
    )
    wx_cert_enc: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="商户私钥 AES加密; PARTNER 模式存【服务商】私钥"
    )
    # 服务商模式字段
    sp_mch_id: Mapped[str] = mapped_column(
        String(50), nullable=False, default="", comment="服务商商户号(平台); DIRECT 模式空"
    )
    sub_mch_id: Mapped[str] = mapped_column(
        String(50), nullable=False, default="", comment="子商户号(特约商户); DIRECT 模式空"
    )
    sub_appid: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="",
        comment="子商户AppID(=pf_tenant.wx_appid); DIRECT 模式空",
    )
    notify_url: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="",
        comment="回调地址; PARTNER 模式统一填【服务商】回调域名",
    )
    enabled: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("tenant_id", name="uk_tenant"),
        {"comment": "支付配置(每租户一行, 支持直连/服务商双模式)"},
    )


class McMsgConfig(Base, IdMixin, TimestampMixin):
    """商家消息配置。"""

    __tablename__ = "mc_msg_config"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    template_no: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="引用 pf_msg_template"
    )
    enabled: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    channels: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, comment='["WX_SUBSCRIBE","INTERNAL"]'
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "template_no", name="uk_tenant_template"),
        {"comment": "商家消息配置"},
    )


class McNotice(Base, IdMixin, TimestampMixin):
    """站内信（保留90天）。"""

    __tablename__ = "mc_notice"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    receiver_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="STAFF/MEMBER"
    )
    receiver_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True, comment="NULL=全员广播"
    )
    type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="ORDER/REFUND/SYSTEM/MARKETING"
    )
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    link: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", comment="跳转路径"
    )
    is_read: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    read_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)

    __table_args__ = (
        Index(
            "idx_receiver",
            "tenant_id",
            "receiver_type",
            "receiver_id",
            "is_read",
            "created_at",
        ),
        {"comment": "站内信(保留90天)"},
    )


for _m in (McRole, McShop, McStore, McPayConfig, McMsgConfig, McNotice):
    register_tenant_model(_m)
