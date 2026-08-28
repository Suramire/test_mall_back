"""补齐 od_order 积分相关列（支付抵扣 pay_points / 已发积分 earned_points）。

模型 app/models/od_order.py 已定义 pay_points、earned_points 两列，但既有
MySQL 实例可能尚未同步，导致真实环境支付/发放积分写库 500。
本迁移补列（带存在性守卫，可重复执行不报错），并为幂等发放提供字段支撑。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_order_points_columns"
down_revision: str | None = "0007_refund_original_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = [r[0] for r in bind.execute(sa.text("SHOW COLUMNS FROM od_order"))]
    if "pay_points" not in cols:
        op.add_column(
            "od_order",
            sa.Column("pay_points", sa.Integer(), nullable=False, server_default="0"),
        )
    if "earned_points" not in cols:
        op.add_column(
            "od_order",
            sa.Column("earned_points", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    cols = [r[0] for r in bind.execute(sa.text("SHOW COLUMNS FROM od_order"))]
    if "earned_points" in cols:
        op.drop_column("od_order", "earned_points")
    if "pay_points" in cols:
        op.drop_column("od_order", "pay_points")
