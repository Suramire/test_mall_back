"""补齐 od_order.idempotency_key 列（下单 Idempotency-Key 幂等去重）。

0001_initial 采用 metadata.create_all 建库，模型后续新增的
idempotency_key（app/models/od_order.py）未同步到既有 MySQL 实例，
导致真实环境带 Idempotency-Key 的 POST /api/c/order/create 直接 500
(1054 Unknown column 'od_order.idempotency_key')。本迁移补列 + 索引。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_order_idempotency_key"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = [r[0] for r in bind.execute(sa.text("SHOW COLUMNS FROM od_order"))]
    if "idempotency_key" not in cols:
        op.add_column(
            "od_order",
            sa.Column("idempotency_key", sa.String(length=80), nullable=True),
        )
        op.create_index(
            "ix_od_order_idempotency_key", "od_order", ["idempotency_key"]
        )


def downgrade() -> None:
    op.drop_index("ix_od_order_idempotency_key", table_name="od_order")
    op.drop_column("od_order", "idempotency_key")
