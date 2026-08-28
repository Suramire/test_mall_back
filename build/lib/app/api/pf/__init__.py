"""平台端路由聚合。/api/pf 前缀在 router.py 统一挂载。"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.pf import auth, audit, dashboard, feature_tree, merchant, member, role, staff, msg_template

pf_router = APIRouter()
pf_router.include_router(merchant.export_router)
pf_router.include_router(merchant.batch_router)
pf_router.include_router(merchant.detail_router)
pf_router.include_router(auth.router)
pf_router.include_router(merchant.router)
pf_router.include_router(dashboard.router)
pf_router.include_router(feature_tree.router)
pf_router.include_router(role.router)
pf_router.include_router(staff.router)
pf_router.include_router(audit.router)
pf_router.include_router(msg_template.router)
pf_router.include_router(member.router)
