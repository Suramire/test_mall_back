"""平台端商家（租户）管理 /api/pf/merchant。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Body, Request

from app.core.deps import require_perms
from app.core.response import ok, page as page_response
from app.db.session import SessionLocal
from app.schemas import (
    ImpersonateResp,
    OpenAccountReq,
    OpenAccountResp,
    RenewReq,
    UpdateTenantReq,
)
from app.services import tenant as tenant_svc

router = APIRouter(prefix="/merchant", tags=["平台-商家管理"])
export_router = APIRouter(prefix="/merchant", tags=["平台-商家管理"])
batch_router = APIRouter(prefix="/merchant", tags=["平台-商家管理"])
detail_router = APIRouter(prefix="/merchant", tags=["平台-商家管理"])


@router.get("")
def list_merchants(
    keyword: str | None = None,
    status: str | None = None,
    expireStart: str | None = None,
    expireEnd: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    sortBy: str | None = None,
    sortOrder: str = "desc",
    _: None = Depends(require_perms("PF_MERCHANT_LIST")),
):
    with SessionLocal() as session:
        data = tenant_svc.list_tenants(
            session, keyword=keyword, status=status,
            expire_start=expireStart, expire_end=expireEnd, page=page, size=size,
            sort_by=sortBy, sort_order=sortOrder,
        )
        return page_response(data["list"], data["total"], data["page"], data["size"])

@export_router.get("/export")
def export_merchants(_: None = Depends(require_perms("PF_MERCHANT_LIST"))):
    # 导出任务由异步 worker 处理；MVP 返回可下载数据快照
    with SessionLocal() as session:
        return ok(tenant_svc.list_tenants(session, keyword=None,status=None,expire_start=None,expire_end=None,page=1,size=10000)["list"])


@router.get("/{tenant_id}")
def merchant_detail(tenant_id: int, _: None = Depends(require_perms("PF_MERCHANT_LIST"))):
    with SessionLocal() as session:
        return ok(tenant_svc.get_tenant_detail(session, tenant_id))

@detail_router.get("/{tenant_id}/detail")
def merchant_detail_alias(tenant_id: int, _: None = Depends(require_perms("PF_MERCHANT_LIST"))):
    with SessionLocal() as session:
        return ok(tenant_svc.get_tenant_detail(session, tenant_id))


@router.post("")
def open_merchant(req: OpenAccountReq, _: None = Depends(require_perms("PF_MERCHANT_EDIT"))):
    with SessionLocal() as session:
        result = tenant_svc.open_account(session, req)
        session.commit()
        return ok(OpenAccountResp(
            id=result["id"],
            tenantNo=result["tenant_no"],
            adminInitPassword=result["admin_init_password"],
        ).model_dump())


@router.put("/{tenant_id}")
def update_merchant(tenant_id: int, req: UpdateTenantReq, _: None = Depends(require_perms("PF_MERCHANT_EDIT"))):
    with SessionLocal() as session:
        tenant_svc.update_tenant(session, tenant_id, req)
        session.commit()
        return ok()


@router.post("/{tenant_id}/disable")
def disable_merchant(tenant_id: int, _: None = Depends(require_perms("PF_MERCHANT_STATUS"))):
    with SessionLocal() as session:
        tenant_svc.set_status(session, tenant_id, "DISABLED")
        session.commit()
        return ok()

@batch_router.post("/batch-status")
def batch_status(ids: list[int] = Body(...), status: str = "DISABLED", _: None = Depends(require_perms("PF_MERCHANT_STATUS"))):
    if status not in ("NORMAL", "DISABLED", "EXPIRED"): status = "DISABLED"
    with SessionLocal() as session:
        for tenant_id in ids: tenant_svc.set_status(session, tenant_id, status)
        session.commit(); return ok({"updated": len(ids), "status": status})


@router.post("/{tenant_id}/enable")
def enable_merchant(tenant_id: int, _: None = Depends(require_perms("PF_MERCHANT_STATUS"))):
    with SessionLocal() as session:
        tenant_svc.set_status(session, tenant_id, "NORMAL")
        session.commit()
        return ok()


@router.post("/{tenant_id}/renew")
def renew_merchant(tenant_id: int, req: RenewReq, _: None = Depends(require_perms("PF_MERCHANT_EDIT"))):
    with SessionLocal() as session:
        tenant_svc.renew(session, tenant_id, req.expireAt.isoformat())
        session.commit()
        return ok()


@router.get("/{tenant_id}/features")
def merchant_features(tenant_id: int, _: None = Depends(require_perms("PF_MERCHANT_LIST"))):
    with SessionLocal() as session:
        return ok(tenant_svc.get_features(session, tenant_id))


@router.put("/{tenant_id}/features")
def set_merchant_features(tenant_id: int, codes: list[str], _: None = Depends(require_perms("PF_MERCHANT_EDIT"))):
    with SessionLocal() as session:
        tenant_svc.set_features(session, tenant_id, codes)
        session.commit()
        return ok()


@router.post("/{tenant_id}/impersonate")
def impersonate(tenant_id: int, request: Request, _: None = Depends(require_perms("PF_MERCHANT_IMPERSONATE"))):
    """代客登录：生成一次性 ticket + 跳转 URL（60s 有效）。"""
    with SessionLocal() as session:
        from app.models.pf_tenant import PfTenant
        from app.core.exceptions import ForbiddenError
        t = session.get(PfTenant, tenant_id)
        if not t:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("租户不存在")
        if t.status in ("EXPIRED", "DISABLED"):
            raise ForbiddenError("租户已到期或禁用，禁止代客登录")
        import secrets
        ticket = secrets.token_hex(16)
        # ticket 存入 Redis 60s（降级：仅返回）
        from app.core.redis import set_kv
        set_kv(f"imp:{ticket}", str(tenant_id), ttl=60)
        from app.services.audit import write_audit

        write_audit(
            session, action="MERCHANT_IMPERSONATE", target_type="pf_tenant", target_id=str(tenant_id),
            tenant_id=tenant_id,
            ip=request.client.host if request.client else "",
        )
        session.commit()
        return ok(ImpersonateResp(redirectUrl=f"http://localhost:3002/sso/callback?ticket={ticket}", ticket=ticket).model_dump())
