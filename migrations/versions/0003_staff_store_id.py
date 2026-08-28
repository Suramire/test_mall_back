"""add optional merchant staff store ownership"""
from alembic import op
import sqlalchemy as sa

revision = "0003_staff_store_id"
down_revision = "0002_order_idempotency_key"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("mc_staff", sa.Column("store_id", sa.BigInteger(), nullable=True, comment="员工归属门店，NULL=全店/管理员"))
    op.create_index("idx_mc_staff_store", "mc_staff", ["tenant_id", "store_id"])

def downgrade() -> None:
    op.drop_index("idx_mc_staff_store", table_name="mc_staff")
    op.drop_column("mc_staff", "store_id")
