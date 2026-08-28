"""声明式基类。统一软删除字段 deleted_at 与时间戳。

类型口径（对齐 docs/architecture/02-数据库设计.md §0 全局约定）：
- 主键/外键引用列 BIGINT UNSIGNED
- 时间戳 DATETIME(3)，UTC 存储；updated_at 带 ON UPDATE CURRENT_TIMESTAMP(3)
- 布尔标志位 TINYINT

注意：SQLAlchemy 通用 `DateTime(3)` 的位置参数是 timezone 而非小数秒精度，
在 MySQL 上会退化成无精度的 DATETIME。故统一使用下方 DT3 (mysql.DATETIME(fsp=3))，
并用 with_variant 保证 SQLite（单元测试）等其他方言仍可编译。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateIndex
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql.expression import ColumnElement

# DATETIME(3)：MySQL 用 fsp=3，其他方言退回通用 DateTime
DT3 = mysql.DATETIME(fsp=3).with_variant(DateTime(), "sqlite")
# BIGINT UNSIGNED：其他方言退回通用 BigInteger
BIGINT_U = mysql.BIGINT(unsigned=True).with_variant(BigInteger(), "sqlite")
# TINYINT：布尔标志位
TINYINT = mysql.TINYINT().with_variant(Integer(), "sqlite")

# 服务端默认值 / 自动更新。
# MySQL 需要 CURRENT_TIMESTAMP(3) 与 ON UPDATE 子句；SQLite（单元测试用内存库）既不支持
# 带精度的 CURRENT_TIMESTAMP，也不支持 ON UPDATE。text() 无 with_variant，故用自定义
# 编译元素按方言下发不同 SQL。


class _Now3(ColumnElement):
    """server_default：MySQL 下 CURRENT_TIMESTAMP(3)，其他方言 CURRENT_TIMESTAMP。"""

    inherit_cache = True


class _Now3OnUpdate(ColumnElement):
    """server_default + ON UPDATE：仅 MySQL 生成 ON UPDATE 子句。"""

    inherit_cache = True


@compiles(_Now3)
def _compile_now3(element, compiler, **kw) -> str:  # noqa: ARG001
    return "CURRENT_TIMESTAMP"


@compiles(_Now3, "mysql")
def _compile_now3_mysql(element, compiler, **kw) -> str:  # noqa: ARG001
    return "UTC_TIMESTAMP(3)"


@compiles(_Now3OnUpdate)
def _compile_now3_upd(element, compiler, **kw) -> str:  # noqa: ARG001
    return "CURRENT_TIMESTAMP"


@compiles(_Now3OnUpdate, "mysql")
def _compile_now3_upd_mysql(element, compiler, **kw) -> str:  # noqa: ARG001
    return "UTC_TIMESTAMP(3) ON UPDATE UTC_TIMESTAMP(3)"


NOW3 = _Now3()
_ON_UPDATE_NOW3 = _Now3OnUpdate()


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    pass


@compiles(CreateIndex, "sqlite")
def _sqlite_unique_index_name(element, compiler, **kw):
    """SQLite 索引名是库级全局唯一，MySQL 是表级唯一。

    DDL 口径（02-数据库设计.md）中 idx_order / idx_member / idx_ref 等在多张表重复，
    在 MySQL 完全合法，但 SQLite 建表会报 "index already exists"，导致单元测试
    Base.metadata.create_all(sqlite) 失败。此处仅在 SQLite 方言下把索引名加表名前缀，
    不影响 MySQL 迁移产出的真实索引名。
    """
    idx = element.element
    # FULLTEXT ... WITH PARSER ngram 是 MySQL 专属，SQLite 直接跳过
    if idx.dialect_options.get("mysql", {}).get("prefix") == "FULLTEXT":
        return "SELECT 1"
    original = idx.name
    idx.name = f"{idx.table.name}_{original}"
    try:
        return compiler.visit_create_index(element, **kw)
    finally:
        idx.name = original


class TimestampMixin:
    """统一时间戳：created_at / updated_at (UTC, DATETIME(3))。"""

    created_at: Mapped[datetime] = mapped_column(
        DT3, nullable=False, server_default=NOW3
    )
    updated_at: Mapped[datetime] = mapped_column(
        DT3,
        nullable=False,
        server_default=_ON_UPDATE_NOW3,
    )


class CreatedAtMixin:
    """仅创建时间戳。用于追加写日志表（gd_stock_log / mb_points_log / sys_file 等）。"""

    created_at: Mapped[datetime] = mapped_column(
        DT3, nullable=False, server_default=NOW3
    )


class SoftDeleteMixin:
    """软删除：deleted_at DATETIME(3) NULL。查询默认过滤 deleted_at IS NULL。"""

    deleted_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True, default=None)


class TenantMixin:
    """多租户业务表：首列业务字段 tenant_id，所有索引以其打头。"""

    tenant_id: Mapped[int] = mapped_column(BIGINT_U, nullable=False, index=True)


class IdMixin:
    id: Mapped[int] = mapped_column(BIGINT_U, primary_key=True, autoincrement=True)
