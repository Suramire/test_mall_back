"""数据库维护脚本：迁移 + seed。

用法：
  python -m app.db.commands migrate    # alembic upgrade head
  python -m app.db.commands seed       # 写入平台初始数据
  python -m app.db.commands reset      # 清空所有表（仅开发）
"""
from __future__ import annotations

import subprocess
import sys


def migrate() -> None:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)


def seed() -> None:
    from app.db.seed import run_seed
    print("Seed 完成:", run_seed())


def reset() -> None:
    subprocess.run([sys.executable, "-m", "alembic", "downgrade", "base"], check=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "migrate"
    if cmd == "migrate":
        migrate()
    elif cmd == "seed":
        seed()
    elif cmd == "reset":
        reset()
    else:
        print("usage: python -m app.db.commands [migrate|seed|reset]")
