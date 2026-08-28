"""od_refund 新增 order_status_before 列（驳回时还原订单原始状态）。

退款申请创建时记录订单当时的 status，驳回(reject)时据此还原，
避免一律回退 PAID 导致已发货(SHIPPED)/待收货(PENDING_RECEIVE)订单状态机错乱。
新列可 NULL 以兼容存量退款单（无记录时驳回回退 PAID）。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_refund_original_status"
down_revision: str | None = "0006_phone_enc_encrypt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = [r[0] for r in bind.execute(sa.text("SHOW COLUMNS FROM od_refund"))]
    if "order_status_before" not in cols:
        op.add_column(
            "od_refund",
            sa.Column("order_status_before", sa.String(length=32), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("od_refund", "order_status_before")
