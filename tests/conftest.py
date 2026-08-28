"""T-901 验收公共夹具（基座横切）。

测试环境策略（经 pm 协调待 be-dev 确认，当前采用可独立运行方案）：
- DB：SQLite 内存库（sqlite:///:memory:），每次测试事务回滚，避免依赖本地 MySQL。
- Redis：fakeredis 内存模式（若未安装则降级为本地 dict 桩），避免依赖本地 Redis。
- 基座钩子：对 engine 安装 install_tenant_hooks，启用多租户强制隔离。

be-dev 交付任意一块基座代码后，本 conftest 可直接驱动验收。
"""
from __future__ import annotations

import os
import sys
from collections.abc import Generator

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# 在测试导入前强制使用 SQLite 内存库，覆盖 config 中的 MySQL 默认
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://fake/0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 让 backend 的 db/session.py 在 SQLite 下不传 MySQL 专属 pool 参数
# （be-dev 基座为 MySQL8 设计，传 pool_size/max_overflow 给 SQLite 会 TypeError）
_real_create_engine = create_engine


def _patched_create_engine(url, **kwargs):
    if str(url).startswith("sqlite"):
        kwargs.pop("pool_size", None)
        kwargs.pop("max_overflow", None)
        kwargs.pop("pool_recycle", None)
        kwargs.pop("pool_pre_ping", None)
    return _real_create_engine(url, **kwargs)


import sqlalchemy as _sa

_sa.create_engine = _patched_create_engine  # type: ignore[misc]
sys.modules["sqlalchemy"].create_engine = _patched_create_engine

from app.core import errors  # noqa: E402
from app.core.redis import _redis as _redis_slot  # noqa: E402
from app.db import orm_hooks  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.core.security import create_access_token, SCOPE_PLATFORM, SCOPE_MERCHANT  # noqa: E402

# 必须在 Base.metadata.create_all 之前 import，确保所有 ORM 模型已注册到 metadata
import app.models  # noqa: E402


# ---------------------------------------------------------------------------
# 内存 Redis 桩（fakeredis 不可用时的兜底）
# ---------------------------------------------------------------------------
class _LocalRedisStub:
    """极简内存 KV，满足 idempotency/blacklist/lock 接口。"""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float | None]] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self._store:
            return None
        self._store[key] = (value, ex)
        return True

    def setex(self, key, ttl, value):
        self._store[key] = (value, ttl)
        return True

    def get(self, key):
        return self._store.get(key, (None, None))[0]

    def exists(self, key):
        return key in self._store

    def delete(self, key):
        return self._store.pop(key, None) is not None

    def incr(self, key):
        cur = int(self._store.get(key, ("0", None))[0]) + 1
        self._store[key] = (str(cur), None)
        return cur

    def ping(self):
        return True


@pytest.fixture(scope="session", autouse=True)
def _patch_redis():
    """用 fakeredis 或本地桩替换全局 Redis 连接。"""
    try:
        r = fakeredis.FakeStrictRedis(decode_responses=True)
    except Exception:
        r = _LocalRedisStub()  # type: ignore[assignment]
    import app.core.redis as redis_mod

    redis_mod._redis = r  # type: ignore[misc]
    yield
    redis_mod._redis = None  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 测试 DB（SQLite 内存，事务回滚）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite 启用外键约束（与 MySQL 行为对齐）
    @event.listens_for(eng, "connect")
    def _fk_pragma(dbapi_con, _):
        cur = dbapi_con.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    # 安装多租户 ORM 钩子 —— 必须走真实产品代码路径（install_tenant_hooks），
    # 不可手动 _sa_event.listen 绕行，否则验的是测试自己挂的钩子而非产品装配逻辑
    # （BUG-001 当初正是因这种绕行被放过）。install_tenant_hooks 现已修复为挂 Session 级事件。
    orm_hooks.install_tenant_hooks()
    # 触发全部 ORM 模型注册到 Base.metadata（含 T-021 业务表 mc_*/gd_*/mb_*/od_*/sys_*），
    # 否则 create_all 仅建部分表，导致 test_cross_tenant_write_rejected 报 no such table。
    import app.models  # noqa: F401
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db_session(engine) -> Generator[Session, None, None]:
    """事务回滚式 session：每个测试独立、互不污染。"""
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def app(engine):
    """构造测试用 FastAPI app（挂载基座中间件 + 路由）。"""
    from fastapi import FastAPI
    from app.core.handlers import register_exception_handlers
    from app.middleware.trace import TraceIdMiddleware
    from app.middleware.tenant_guard import TenantGuardMiddleware
    from app.middleware.utc_timestamps import UtcTimestampMiddleware

    application = FastAPI()
    application.add_middleware(TenantGuardMiddleware)
    application.add_middleware(TraceIdMiddleware)
    application.add_middleware(UtcTimestampMiddleware)
    register_exception_handlers(application)

    # 注入测试 engine：替换 app.db.session 的模块级 engine/SessionLocal 为 SQLite
    from app.db import session as db_session_mod
    from sqlalchemy import event as _sa_event

    db_session_mod.engine = engine
    db_session_mod.SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    # 确保业务代码用到的 scoped_session 也指向测试 engine
    db_session_mod.db_session.configure(bind=engine)

    def _override_get_db():
        TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    from app.api.router import api_router

    application.include_router(api_router, prefix="/api")
    application.dependency_overrides[db_session_mod.get_db] = _override_get_db
    return application


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# 认证头构造（平台端 / 商家端）
# ---------------------------------------------------------------------------
@pytest.fixture
def platform_token() -> str:
    """平台员工 JWT（无 tenant_id）。"""
    return create_access_token(
        subject="1",
        scope=SCOPE_PLATFORM,
        perms=["PF_DASHBOARD", "PF_MERCHANT_LIST", "PF_MERCHANT_EDIT",
               "PF_MERCHANT_STATUS", "PF_MERCHANT_IMPERSONATE", "PF_ROLE", "PF_STAFF"],
    )


@pytest.fixture
def merchant_token() -> str:
    """商家端 JWT（含 tenant_id=1001）。"""
    return create_access_token(
        subject="10",
        scope=SCOPE_MERCHANT,
        tenant_id=1001,
        features=["goods.manage", "order.view"],
    )


@pytest.fixture
def auth_headers(platform_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {platform_token}"}


# ---------------------------------------------------------------------------
# 业务码断言辅助
# ---------------------------------------------------------------------------
def assert_biz_code(resp, expected_code: int):
    """断言响应体中的业务 code 与预期一致（HTTP 应恒为 200）。"""
    assert resp.status_code == 200, f"期望 HTTP 200，实际 {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["code"] == expected_code, (
        f"期望 code={expected_code}，实际 code={body['code']} msg={body.get('message')}"
    )
    return body


def assert_ok(resp):
    return assert_biz_code(resp, errors.BizCode.OK)


@pytest.fixture(autouse=True)
def _register_tenant_models():
    """确保已定义业务模型注册到租户钩子（若有）。"""
    yield
