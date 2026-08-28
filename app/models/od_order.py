"""交易域模型：od_order / od_order_item / od_payment / od_refund / od_verify_code / od_cart / od_address。

DDL 口径见 docs/architecture/02-数据库设计.md §5。
要点：
- od_order 10 态状态机；idx_pay_deadline / idx_auto_receive 为跨租户扫描索引，
  Celery 定时任务使用时须显式 execution_options(skip_tenant_filter=True)。
- od_order_item 全字段快照（商品名/规格/图/单价/分类），避免商品变更影响历史订单。
- od_payment.transaction_id 唯一 = 支付回调幂等最终防线。
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BIGINT_U, DT3, Base, IdMixin, SoftDeleteMixin, TimestampMixin
from app.db.orm_hooks import register_tenant_model

_ZERO = Decimal("0.00")


class OdOrder(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """订单（10 态状态机）。channel 为业务线，创建后不可变更。"""

    __tablename__ = "od_order"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    order_no: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="ORD/PT + YYYYMMDD + 6位"
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="NORMAL/POINTS 业务线,不可变更"
    )
    member_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    member_no: Mapped[str] = mapped_column(
        String(20), nullable=False, default="", comment="快照"
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="OrderStatus 10态"
    )
    delivery_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="EXPRESS/PICKUP/VERIFY/INSTANT"
    )
    # 金额
    goods_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=_ZERO
    )
    freight_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=_ZERO
    )
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=_ZERO, comment="会员折扣"
    )
    pay_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=_ZERO, comment="实付现金"
    )
    pay_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pay_method: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="",
        comment="WECHAT/POINTS/POINTS_WECHAT",
    )
    level_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True, comment="下单时刻等级快照"
    )
    discount_rate: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, comment="快照"
    )
    # 收货/自提/核销
    receiver_name: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    receiver_phone: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    receiver_address: Mapped[str] = mapped_column(
        String(500), nullable=False, default=""
    )
    store_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True)
    store_name: Mapped[str] = mapped_column(
        String(100), nullable=False, default="", comment="快照"
    )
    pickup_code: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="ZTD######"
    )
    # 物流
    express_company: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    express_no: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    # 积分
    earned_points: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="本单已发放(回滚依据)"
    )
    # 时间节点
    pay_deadline: Mapped[datetime | None] = mapped_column(DT3, nullable=True, comment="下单+30min"
    )
    paid_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)
    stocked_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)
    # 其他
    buyer_remark: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    cancel_reason: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    operator_ship: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    operator_verify: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    operator_pickup: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("tenant_id", "order_no", name="uk_order_no"),
        UniqueConstraint("tenant_id", "pickup_code", name="uk_pickup_code"),
        Index("idx_member", "tenant_id", "member_id", "created_at"),
        Index("idx_status", "tenant_id", "status", "created_at"),
        Index("idx_ch_status", "tenant_id", "channel", "status", "created_at"),
        Index("idx_store", "tenant_id", "store_id", "status"),
        # ★跨租户扫描索引（定时任务用，不带 tenant_id）
        Index("idx_pay_deadline", "status", "pay_deadline"),
        Index("idx_auto_receive", "status", "shipped_at"),
        {"comment": "订单"},
    )


class OdOrderItem(Base, IdMixin, TimestampMixin):
    """订单明细（全字段快照）。"""

    __tablename__ = "od_order_item"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    order_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    goods_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    sku_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    goods_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="★快照"
    )
    goods_type: Mapped[str] = mapped_column(String(20), nullable=False)
    spec_text: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", comment="★快照"
    )
    image: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", comment="★快照"
    )
    category_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True, comment="★快照,积分分类倍率按行计算"
    )
    price: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=_ZERO, comment="★单价快照"
    )
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    subtotal_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=_ZERO
    )
    subtotal_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    refund_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="NONE"
    )

    __table_args__ = (
        Index("idx_order", "tenant_id", "order_id"),
        Index("idx_goods", "tenant_id", "goods_id"),
        {"comment": "订单明细(全字段快照)"},
    )


class OdPayment(Base, IdMixin, TimestampMixin):
    """支付流水。transaction_id 唯一为回调幂等最终防线。"""

    __tablename__ = "od_payment"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    order_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    out_trade_no: Mapped[str] = mapped_column(
        String(40), nullable=False, comment="本系统支付单号(=order_no或带后缀)"
    )
    transaction_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="微信交易单号"
    )
    pay_method: Mapped[str] = mapped_column(String(20), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=_ZERO
    )
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    refund_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=_ZERO, comment="累计退款"
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    paid_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="微信原始回调,排障用"
    )

    __table_args__ = (
        UniqueConstraint("out_trade_no", name="uk_out_trade_no"),
        UniqueConstraint("transaction_id", name="uk_transaction_id"),
        Index("idx_order", "tenant_id", "order_id"),
        Index("idx_tenant_time", "tenant_id", "status", "paid_at"),
        {"comment": "支付流水"},
    )


class OdRefund(Base, IdMixin, TimestampMixin):
    """退款单。MVP 为整单退款（order_item_id 为 NULL）。"""

    __tablename__ = "od_refund"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    order_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    order_item_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True, comment="P2 部分退款预留,MVP为NULL(整单)"
    )
    refund_no: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="RF+YYYYMMDD+6位"
    )
    refund_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=_ZERO
    )
    refund_points: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="退回积分(兑换单)"
    )
    rollback_points: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="回滚扣除积分"
    )
    rollback_debt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="余额不足产生的欠账"
    )
    reason_code: Mapped[str] = mapped_column(String(30), nullable=False)
    reason_desc: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    images: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="凭证图"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PENDING_AUDIT"
    )
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="USER",
        comment="USER/SYSTEM(过期自动退)",
    )
    audit_staff_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True)
    audit_staff_name: Mapped[str] = mapped_column(
        String(50), nullable=False, default=""
    )
    audit_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)
    reject_reason: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    order_status_before: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="申请退款前订单原始状态，驳回时还原"
    )
    refund_channel: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ORIGINAL"
    )
    wx_refund_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    finished_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "refund_no", name="uk_refund_no"),
        Index("idx_order", "tenant_id", "order_id"),
        Index("idx_status", "tenant_id", "status", "created_at"),
        {"comment": "退款单"},
    )


class OdVerifyCode(Base, IdMixin, TimestampMixin):
    """核销码/券码（购买 N 张生成 N 条）。"""

    __tablename__ = "od_verify_code"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    order_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    order_item_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    member_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    code: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="HX+12位 / VC+日期+6位"
    )
    code_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="VERIFY/VIRTUAL"
    )
    goods_name: Mapped[str] = mapped_column(String(200), nullable=False, comment="快照")
    spec_text: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    valid_start: Mapped[datetime] = mapped_column(DT3, nullable=False)
    valid_end: Mapped[datetime] = mapped_column(DT3, nullable=False)
    applicable_store_ids: Mapped[list[int] | None] = mapped_column(
        JSON, nullable=True, comment="快照,null=全部门店"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="UNUSED",
        comment="UNUSED/USED/EXPIRED/REFUNDED",
    )
    verified_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True)
    verify_store_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True)
    verify_store_name: Mapped[str] = mapped_column(
        String(100), nullable=False, default=""
    )
    verify_staff_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True)
    verify_staff_name: Mapped[str] = mapped_column(
        String(50), nullable=False, default=""
    )
    expire_refund_policy: Mapped[str] = mapped_column(
        String(30), nullable=False, default="FULL_CASH"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uk_code"),
        Index("idx_order", "tenant_id", "order_id"),
        # ★跨租户定时扫描过期
        Index("idx_status_valid", "status", "valid_end"),
        Index("idx_verify_log", "tenant_id", "verify_store_id", "verified_at"),
        {"comment": "核销码/券码(购买N张生成N条)"},
    )


class OdCart(Base, IdMixin, TimestampMixin):
    """购物车（仅 NORMAL 渠道；积分商城直接兑换不入车）。失效状态实时计算不落库。"""

    __tablename__ = "od_cart"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    member_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    goods_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    sku_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    channel: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="NORMAL",
        comment="★购物车仅NORMAL,积分商城直接兑换",
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    selected: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "member_id", "sku_id", "channel", name="uk_member_sku"
        ),
        Index("idx_member", "tenant_id", "member_id"),
        {"comment": "购物车(失效状态实时计算,不落库)"},
    )


class OdAddress(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """收货地址。"""

    __tablename__ = "od_address"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    member_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    receiver_name: Mapped[str] = mapped_column(String(50), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    province: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    city: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    district: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    detail: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_default: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_member", "tenant_id", "member_id", "is_default"),
        {"comment": "收货地址"},
    )


for _m in (
    OdOrder,
    OdOrderItem,
    OdPayment,
    OdRefund,
    OdVerifyCode,
    OdCart,
    OdAddress,
):
    register_tenant_model(_m)
