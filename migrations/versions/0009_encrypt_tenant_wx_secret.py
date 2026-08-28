"""Encrypt legacy pf_tenant.wx_secret_enc plaintext values.

Revision ID: 0009_encrypt_tenant_wx_secret
Revises: 0008_order_points_columns
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.crypto_secret import decrypt_secret, encrypt_secret

revision: str = "0009_encrypt_tenant_wx_secret"
down_revision: str | None = "0008_order_points_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    for tenant_id, value in bind.execute(sa.text("SELECT id, wx_secret_enc FROM pf_tenant")).fetchall():
        if value and not value.startswith("sec:"):
            bind.execute(
                sa.text("UPDATE pf_tenant SET wx_secret_enc=:value WHERE id=:id"),
                {"id": tenant_id, "value": encrypt_secret(value)},
            )


def downgrade() -> None:
    bind = op.get_bind()
    for tenant_id, value in bind.execute(sa.text("SELECT id, wx_secret_enc FROM pf_tenant")).fetchall():
        if value and value.startswith("sec:"):
            bind.execute(
                sa.text("UPDATE pf_tenant SET wx_secret_enc=:value WHERE id=:id"),
                {"id": tenant_id, "value": decrypt_secret(value)},
            )
