"""SQLAlchemy 同步 Engine + scoped_session。

按定稿约定：services/** 一律写同步函数，API 层通过 run_in_threadpool 调用，
一份 service 代码同时供 API 与 Celery worker 使用。
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=10,
    max_overflow=20,
    connect_args={"init_command": "SET time_zone = '+00:00'"},
    echo=settings.APP_DEBUG,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)

# scoped_session：线程/异步隔离，供 Celery worker 使用
db_session = scoped_session(SessionLocal)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求级会话。"""
    session: Session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
