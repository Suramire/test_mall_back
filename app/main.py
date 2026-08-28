"""应用入口。挂载中间件、异常处理、路由。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.handlers import register_exception_handlers
from app.db.orm_hooks import install_tenant_hooks
from app.db.session import engine
from app.middleware.trace import TraceIdMiddleware
from app.middleware.tenant_guard import TenantGuardMiddleware
from app.middleware.utc_timestamps import UtcTimestampMiddleware

# 安装多租户强制隔离钩子（Session 级事件，幂等）
install_tenant_hooks()

app = FastAPI(
    title=settings.APP_NAME,
    debug=settings.APP_DEBUG,
    openapi_url="/api/openapi.json",
    docs_url="/api/docs" if settings.APP_DEBUG else None,
    redoc_url=None,
)

# 中间件（后添加的先执行）：TraceId → TenantGuard
app.add_middleware(TenantGuardMiddleware)
app.add_middleware(TraceIdMiddleware)
app.add_middleware(UtcTimestampMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

app.include_router(api_router, prefix="/api")


@app.get("/", include_in_schema=False)
async def root():
    return {"service": settings.APP_NAME, "status": "ok"}


@app.get("/health", include_in_schema=False)
async def health():
    """无前缀健康检查（负载均衡/探活用）。完整检查见 /api/common/health。"""
    return {"status": "healthy"}
