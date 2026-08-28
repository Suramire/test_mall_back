"""商品域模型：gd_category / gd_goods / gd_sku / gd_sku_stock / gd_stock_log / gd_freight_template。

DDL 口径见 docs/architecture/02-数据库设计.md §3。
要点：
- 双渠道（NORMAL/POINTS）独立分类树、独立上下架开关、独立库存。
- gd_sku_stock 为三段式库存（total/locked/sold/available），available 由应用维护便于乐观锁。
- SOLD_OUT 为派生状态不落库。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT as _MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

# MEDIUMTEXT：SQLite（单元测试内存库）退回通用 Text
MEDIUMTEXT = _MEDIUMTEXT().with_variant(Text(), "sqlite")

from app.db.base import BIGINT_U, DT3, TINYINT, Base, CreatedAtMixin, IdMixin, SoftDeleteMixin, TimestampMixin
from app.db.orm_hooks import register_tenant_model


class GdCategory(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """商品分类（NORMAL/POINTS 两套独立分类树）。"""

    __tablename__ = "gd_category"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    channel: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="★NORMAL/POINTS 两套独立分类树"
    )
    parent_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False, default=0, comment="0=顶级"
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    icon: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    sort: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="越大越靠前"
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ENABLED")

    __table_args__ = (
        Index("idx_tenant_channel_parent", "tenant_id", "channel", "parent_id", "sort"),
        {"comment": "商品分类(双渠道独立)"},
    )


class GdGoods(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """商品。status 表达整体生命周期；上下架按渠道独立（normal_on_sale/points_on_sale）。"""

    __tablename__ = "gd_goods"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    subtitle: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="PHYSICAL/VIRTUAL/TICKET"
    )
    channel: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="NORMAL/POINTS/BOTH"
    )
    normal_category_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True)
    points_category_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True)
    main_image: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    images: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    detail: Mapped[str | None] = mapped_column(
        MEDIUMTEXT, nullable=True, comment="富文本(存前已XSS过滤)"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="DRAFT", comment="DRAFT/ON_SALE/OFF_SALE"
    )
    normal_on_sale: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="★按渠道独立上下架"
    )
    points_on_sale: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    has_sku: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    spec_config: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True, comment='规格定义 [{name:"颜色",values:[...]}]'
    )
    freight_template_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True)
    sold_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="真实已售"
    )
    virtual_sold: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="虚拟销量基数"
    )
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 核销券专属
    valid_type: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="FIXED_DATE/DAYS_AFTER_PAY"
    )
    valid_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verify_store_ids: Mapped[list[int] | None] = mapped_column(
        JSON, nullable=True, comment="[] 或 null = 全部门店"
    )
    verify_desc: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    expire_refund_policy: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
        comment="FULL_CASH/CASH_ONLY/POINTS_VOID/NO_REFUND",
    )
    # 虚拟商品专属
    virtual_desc: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # 积分商城专属
    points_limit_per_user: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    points_limit_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_tenant_status", "tenant_id", "status", "sort", "id"),
        Index(
            "idx_tenant_nchannel",
            "tenant_id",
            "normal_on_sale",
            "normal_category_id",
            "sort",
        ),
        Index(
            "idx_tenant_pchannel",
            "tenant_id",
            "points_on_sale",
            "points_category_id",
            "sort",
        ),
        Index("idx_tenant_type", "tenant_id", "type"),
        Index("ft_name", "name", "subtitle", mysql_prefix="FULLTEXT", mysql_with_parser="ngram"),
        {"comment": "商品"},
    )


class GdSku(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """SKU。价格分现金/积分/混合三种模式。"""

    __tablename__ = "gd_sku"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    goods_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    sku_code: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="SKU+商品ID+3位"
    )
    spec_json: Mapped[dict[str, str] | None] = mapped_column(
        JSON, nullable=True, comment='{"颜色":"枪灰","尺寸":"L"}'
    )
    spec_text: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", comment="枪灰 / L (冗余展示)"
    )
    image: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    price: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00"), comment="普通商城售价"
    )
    original_price: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00"), comment="划线价"
    )
    price_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="CASH", comment="CASH/POINTS/MIXED"
    )
    points: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="积分商城所需积分"
    )
    cash: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0.00"),
        comment="积分商城补差现金",
    )
    weight: Mapped[Decimal] = mapped_column(
        Numeric(10, 3), nullable=False, default=Decimal("0.000")
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "sku_code", "deleted_at", name="uk_tenant_skucode"),
        Index("idx_goods", "tenant_id", "goods_id"),
        {"comment": "SKU"},
    )


class GdSkuStock(Base, IdMixin, TimestampMixin):
    """★核心：双渠道独立库存（三段式）。

    available_stock = total_stock - locked_stock - sold_stock，由应用维护以支持乐观锁扣减。
    """

    __tablename__ = "gd_sku_stock"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    goods_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    sku_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    channel: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="NORMAL/POINTS"
    )
    total_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_stock: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="下单未支付"
    )
    sold_stock: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已支付"
    )
    available_stock: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="=total-locked-sold(应用维护,便于乐观锁)",
    )
    warn_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("sku_id", "channel", name="uk_sku_channel"),
        Index("idx_tenant_goods_ch", "tenant_id", "goods_id", "channel"),
        Index("idx_warn", "tenant_id", "warn_stock", "available_stock"),
        CheckConstraint(
            "locked_stock >= 0 AND sold_stock >= 0 AND available_stock >= 0",
            name="ck_stock_nonneg",
        ),
        {"comment": "SKU渠道库存(三段式)"},
    )


class GdStockLog(Base, IdMixin, CreatedAtMixin):
    """库存变更日志。仅 created_at，无 updated_at / 软删（追加写，不修改）。"""

    __tablename__ = "gd_stock_log"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    goods_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    sku_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    change_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="INCREASE/DECREASE/SET/ORDER_LOCK/ORDER_PAY/ORDER_RELEASE/REFUND_RETURN",
    )
    before_val: Mapped[int] = mapped_column(Integer, nullable=False)
    change_val: Mapped[int] = mapped_column(Integer, nullable=False)
    after_val: Mapped[int] = mapped_column(Integer, nullable=False)
    ref_type: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    ref_id: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    operator_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True)
    operator_name: Mapped[str] = mapped_column(
        String(50), nullable=False, default="SYSTEM"
    )
    remark: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    __table_args__ = (
        Index("idx_sku_time", "tenant_id", "sku_id", "created_at"),
        Index("idx_ref", "tenant_id", "ref_type", "ref_id"),
        {"comment": "库存变更日志"},
    )


class GdFreightTemplate(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """运费模板。"""

    __tablename__ = "gd_freight_template"

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="BY_PIECE", comment="BY_PIECE/FIXED"
    )
    first_num: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_fee: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    extra_num: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    extra_fee: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    fixed_fee: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    free_threshold: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0.00"), comment="0=不包邮"
    )
    is_default: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)

    __table_args__ = (
        Index("idx_tenant", "tenant_id", "is_default"),
        {"comment": "运费模板"},
    )


for _m in (GdCategory, GdGoods, GdSku, GdSkuStock, GdStockLog, GdFreightTemplate):
    register_tenant_model(_m)
