"""add points import idempotency key"""
from alembic import op
import sqlalchemy as sa

revision = "0004_points_import_idempotency"
down_revision = "0003_staff_store_id"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("mb_points_import", sa.Column("idempotency_key", sa.String(80), nullable=False, server_default=""))
    op.create_unique_constraint("uk_points_import_tenant_key", "mb_points_import", ["tenant_id", "idempotency_key"])

def downgrade():
    op.drop_constraint("uk_points_import_tenant_key", "mb_points_import", type_="unique")
    op.drop_column("mb_points_import", "idempotency_key")
