"""系统域模型：sys_export_task / sys_file。

DDL 口径见 docs/architecture/02-数据库设计.md §6。
注意：两表 tenant_id 均为 NULLABLE（平台级导出/上传为 NULL），因此**不**注册
register_tenant_model —— 强制注入会使平台侧记录无法写入/查询。
租户隔离由服务层按场景显式过滤 tenant_id。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CHAR, BigInteger, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BIGINT_U, DT3, TINYINT, Base, CreatedAtMixin, IdMixin, TimestampMixin


class SysExportTask(Base, IdMixin, TimestampMixin):
    """异步导出任务。文件保留7天。"""

    __tablename__ = "sys_export_task"

    tenant_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True, comment="平台导出为NULL"
    )
    task_no: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    biz_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="ORDER/MEMBER/POINTS_LOG/PAYMENT/VERIFY_LOG",
    )
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="PENDING",
        comment="PENDING/RUNNING/DONE/FAILED",
    )
    progress: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    download_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    error_msg: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    expire_at: Mapped[datetime | None] = mapped_column(DT3, nullable=True, comment="文件保留7天"
    )
    operator_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True)
    operator_name: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("task_no", name="uk_task_no"),
        Index("idx_tenant_time", "tenant_id", "created_at"),
        Index("idx_expire", "expire_at"),
        {"comment": "异步导出任务"},
    )


class SysFile(Base, IdMixin, CreatedAtMixin):
    """文件记录（追加写）。"""

    __tablename__ = "sys_file"

    tenant_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True)
    biz_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="goods/avatar/logo/banner/excel/refund",
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mime: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    uploader_id: Mapped[int | None] = mapped_column(BIGINT_U, nullable=True)

    __table_args__ = (
        Index("idx_tenant_biz", "tenant_id", "biz_type", "created_at"),
        {"comment": "文件记录"},
    )
