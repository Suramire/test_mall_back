"""初始迁移：依据当前所有 ORM 模型建表。

采用 Base.metadata.create_all / drop_all，保证与模型定义完全一致。
后续模型变更通过新增迁移管理（alembic revision --autogenerate）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.base import Base
import app.models  # noqa: F401  触发模型注册


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind)
