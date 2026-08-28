"""平台端数据概览 /api/pf/dashboard（基础 KPI，P0）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.deps import require_perms
from app.core.response import ok
from app.db.session import SessionLocal
from app.models.pf_tenant import PfTenant
from app.models.mb_member import MbMember
from sqlalchemy import func, select
from datetime import date, timedelta

router = APIRouter(prefix="/dashboard", tags=["平台-看板"])


@router.get("/kpi")
def kpi(_: None = Depends(require_perms("PF_DASHBOARD"))):
    with SessionLocal() as session:
        total = session.scalar(select(func.count()).select_from(PfTenant).where(PfTenant.deleted_at.is_(None))) or 0
        expired = session.scalar(
            select(func.count()).select_from(PfTenant).where(PfTenant.status == "EXPIRED", PfTenant.deleted_at.is_(None))
        ) or 0
        configured = session.scalar(
            select(func.count()).select_from(PfTenant).where(PfTenant.wx_auth_status == 1, PfTenant.deleted_at.is_(None))
        ) or 0
        from datetime import date
        due_soon = session.scalar(
            select(func.count()).select_from(PfTenant).where(
                PfTenant.expire_at.between(date.today(), date(2026, 12, 31)), PfTenant.deleted_at.is_(None)
            )
        ) or 0
        return ok({
            "merchantTotal": {"value": total, "delta": 0},
            "monthGmv": {"value": 0, "delta": 0},
            "configuredMerchants": {"value": configured, "delta": 0},
            "pendingRenewal": {"value": due_soon, "delta": 0},
            # 环比字段占位（交易额需订单域，MVP 先置 0）
            "gmvMonth": 0,
            "gmvMonthMoM": 0,
        })

@router.get("/trend")
def trend(days: int = 7, _: None = Depends(require_perms("PF_DASHBOARD"))):
    days=max(1,min(days,90)); today=date.today()
    with SessionLocal() as session:
        out=[]
        for i in range(days):
            d=today-timedelta(days=days-i-1); nxt=d+timedelta(days=1)
            n=session.scalar(select(func.count()).select_from(PfTenant).where(PfTenant.created_at>=d,PfTenant.created_at<nxt,PfTenant.deleted_at.is_(None))) or 0
            out.append({"date":d.isoformat(),"newTenants":n,"gmv":0})
        return ok(out)

@router.get("/endpoint-open")
def endpoint_open(_: None = Depends(require_perms("PF_DASHBOARD"))):
    with SessionLocal() as session:
        tenants=session.scalar(select(func.count()).select_from(PfTenant).where(PfTenant.deleted_at.is_(None))) or 0
        members=session.scalar(select(func.count()).select_from(MbMember).where(MbMember.deleted_at.is_(None))) or 0
        return ok({"user":{"count":members,"ratio":1 if members else 0},"pc":{"count":tenants,"ratio":1 if tenants else 0},"mp":{"count":tenants,"ratio":1 if tenants else 0}})
