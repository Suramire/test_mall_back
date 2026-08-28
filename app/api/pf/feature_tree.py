"""平台端功能点树 /api/pf/feature-tree 与租户已开通功能。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.deps import require_perms
from app.core.response import ok
from app.db.session import SessionLocal
from app.models.pf_feature import PfFeature
from sqlalchemy import select

router = APIRouter(tags=["平台-功能点树"])


@router.get("/feature-tree")
def feature_tree(
    end: str = Query(..., description="user|merchant_pc|merchant_mp"),
    _: None = Depends(require_perms("PF_MERCHANT_EDIT")),
):
    with SessionLocal() as session:
        rows = session.scalars(
            select(PfFeature).where(PfFeature.end_code == end).order_by(PfFeature.sort)
        ).all()
        # 三级树：l1 -> l2 -> items
        tree: dict = {}
        for f in rows:
            tree.setdefault(f.l1_name, {}).setdefault(f.l2_name, []).append({
                "code": f.code,
                "name": f.l3_name,
                "defaultOn": f.default_on,
            })
        result = []
        for l1, l2map in tree.items():
            groups = [{"l2": l2, "items": items} for l2, items in l2map.items()]
            result.append({"end": end, "l1": l1, "groups": groups})
        return ok(result)
