"""Alembic env：使用 app 的同步 engine + Base.metadata 自动生成迁移。

注意：
- 用 app.db.session.engine，保证与运行时一致的 pool 配置。
- 多租户拦截（install_tenant_hooks）仅作用于 ORM 运行时，迁移阶段不安装，
  以免 autogenerate 误判；平台表(pf_*)与业务表统一由 metadata 管理。
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 alembic 能 import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402,F401  触发所有模型注册（import 副作用）
from app.core.config import settings  # noqa: E402

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from app.db.session import engine

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
