"""会员与积分域模型：mb_level / mb_member / mb_points_rule / mb_points_grant / mb_points_log / mb_points_import。

DDL 口径见 docs/architecture/02-数据库设计.md §4。
要点：
- 手机号加密存 phone_enc，另存 phone_mask（展示）与 phone_hash（HMAC-SHA256 精确检索）。
- mb_points_grant 是 FIFO 过期的记账基础：按 granted_at 升序消耗 remaining。
- points_debt 记录退款回滚导致的欠账。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CHAR,
    BigInteger,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BIGINT_U, DT3, NOW3, TINYINT, Base, CreatedAtMixin, IdMixin, SoftDeleteMixin, TimestampMixin
from app.db.orm_hooks import register_tenant_model
from app.db.types import EncryptedString


class MbLevel(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """会员等级（青铜/白银/黄金/钻石，开户时自动创建4档）。"""

    __tablename__ = "mb_level"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False, comment="1..N")
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    icon: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    up_condition: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="TOTAL_AMOUNT/TOTAL_POINTS; level=1为NULL",
    )
    up_value: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    discount_rate: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, comment="100=无折扣,98=98折"
    )
    points_rate: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), nullable=False, default=Decimal("1.00")
    )
    free_freight: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    benefits_desc: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "level", "deleted_at", name="uk_tenant_level"),
        {"comment": "会员等级"},
    )


class MbMember(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """会员。手机号密文存储 + hash 精确检索 + mask 展示。"""

    __tablename__ = "mb_member"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    member_no: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="XF000001"
    )
    openid: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    unionid: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    nickname: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    avatar: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    phone_enc: Mapped[str] = mapped_column(
        EncryptedString(255), nullable=False, default="", comment="AES-GCM加密手机号"
    )
    phone_mask: Mapped[str] = mapped_column(
        String(20), nullable=False, default="", comment="138****1024"
    )
    phone_hash: Mapped[str] = mapped_column(
        CHAR(64),
        nullable=False,
        default="",
        comment="HMAC-SHA256,用于精确检索/导入匹配",
    )
    gender: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    birthday: Mapped[date | None] = mapped_column(Date, nullable=True)
    level_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True)
    points_balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    points_total_earn: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    points_total_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    points_debt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="退款回滚欠账"
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("0.00"),
        comment="累计消费=成长值",
    )
    total_order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(
        DT3, nullable=False, server_default=NOW3
    )
    last_consume_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ONLINE", comment="ONLINE/IMPORT"
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ENABLED")

    __table_args__ = (
        UniqueConstraint("tenant_id", "member_no", name="uk_member_no"),
        UniqueConstraint("tenant_id", "openid", "deleted_at", name="uk_openid"),
        Index("idx_phone_hash", "tenant_id", "phone_hash"),
        Index("idx_level", "tenant_id", "level_id"),
        Index("idx_joined", "tenant_id", "joined_at"),
        {"comment": "会员"},
    )


class MbPointsRule(Base, IdMixin, TimestampMixin):
    """积分规则（每租户一行）。"""

    __tablename__ = "mb_points_rule"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    earn_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("1.00"), comment="每消费N元"
    )
    earn_points: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="获得M积分"
    )
    category_rate_enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    category_rates: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True, comment="[{categoryId,rate}]"
    )
    expire_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="FOREVER"
    )
    expire_months: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    notify_enabled: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    notify_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    register_points: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="新人注册赠送"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", name="uk_tenant"),
        {"comment": "积分规则(每租户一行)"},
    )


class MbPointsGrant(Base, IdMixin, TimestampMixin):
    """★积分发放批次（FIFO 过期的记账基础）。

    消耗顺序：按 granted_at 升序扣减 remaining；expire_at 为 NULL 表示永久有效。
    """

    __tablename__ = "mb_points_grant"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    member_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False, comment="本批发放量")
    remaining: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="剩余未消耗"
    )
    granted_at: Mapped[datetime] = mapped_column(DT3, nullable=False, server_default=NOW3
    )
    expire_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True, comment="NULL=永久"
    )
    source_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="",
        comment="ORDER/IMPORT/ADJUST/REGISTER",
    )
    source_id: Mapped[str] = mapped_column(String(40), nullable=False, default="")

    __table_args__ = (
        Index("idx_fifo", "tenant_id", "member_id", "remaining", "granted_at"),
        Index("idx_expire", "expire_at", "remaining"),
        {"comment": "积分发放批次(FIFO)"},
    )


class MbPointsLog(Base, IdMixin, CreatedAtMixin):
    """积分流水（追加写）。amount 带符号。"""

    __tablename__ = "mb_points_log"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    member_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    change_type: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="PointsChangeType 9种"
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False, comment="带符号")
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    ref_type: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    ref_id: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    remark: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    operator_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True)
    operator_name: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    __table_args__ = (
        Index("idx_member_time", "tenant_id", "member_id", "created_at"),
        Index("idx_type_time", "tenant_id", "change_type", "created_at"),
        Index("idx_ref", "tenant_id", "ref_type", "ref_id"),
        {"comment": "积分流水"},
    )


class MbPointsImport(Base, IdMixin, TimestampMixin):
    """积分导入批次（单批≤5000行）。"""

    __tablename__ = "mb_points_import"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fail_detail: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True, comment="[{row,phone,reason}]"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PROCESSING"
    )
    operator_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True)
    operator_name: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    __table_args__ = (
        Index("idx_tenant_time", "tenant_id", "created_at"),
        {"comment": "积分导入批次(单批≤5000行)"},
    )


for _m in (
    MbLevel,
    MbMember,
    MbPointsRule,
    MbPointsGrant,
    MbPointsLog,
    MbPointsImport,
):
    register_tenant_model(_m)
