from alembic import op
import sqlalchemy as sa
revision='0005_staff_wecom_userid'; down_revision='0004_points_import_idempotency'; branch_labels=None; depends_on=None
def upgrade():
    op.add_column('mc_staff', sa.Column('wecom_userid', sa.String(100), nullable=False, server_default=''))
    op.execute("UPDATE mc_staff SET wecom_userid=NULL WHERE wecom_userid=''" )
    op.alter_column('mc_staff', 'wecom_userid', existing_type=sa.String(100), nullable=True, server_default=None)
    op.create_unique_constraint('uk_mc_staff_tenant_wecom', 'mc_staff', ['tenant_id','wecom_userid'])
def downgrade():
    op.drop_constraint('uk_mc_staff_tenant_wecom','mc_staff',type_='unique'); op.drop_column('mc_staff','wecom_userid')
