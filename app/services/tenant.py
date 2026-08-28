"""租户（商家）平台服务：开户/列表/详情/启停/续费/功能开通。

开户主链路：
1. 生成租户编号 Mxxxx（id_generator.next_tenant_no）
2. 建 pf_tenant（含 wx_secret AES 加密占位）
3. 开通默认功能点（features 空则取 pf_feature.default_on=1）
4. 建商家管理员 mc_staff(is_admin=1) + 生成随机初始密码（仅开户时返回一次）
5. 写审计 TENANT_OPEN / TENANT_FEATURE_CHANGE
"""
from __future__ import annotations

import secrets
import string
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import TenantStatus
from app.core.crypto_secret import encrypt_secret
from app.core.exceptions import ConflictError, NotFoundError, ParamError
from app.core.id_generator import next_tenant_no
from app.core.security import hash_password
from app.core.tenant_context import get_staff_id, get_staff_name, set_tenant
from app.models.gd_goods import GdGoods
from app.models.mb_member import MbMember
from app.models.mc_staff import McStaff
from app.models.mc_config import McStore
from app.models.od_order import OdOrder
from app.models.pf_feature import PfFeature
from app.models.pf_tenant import PfTenant
from app.models.pf_tenant_feature import PfTenantFeature
from app.schemas import OpenAccountReq, UpdateTenantReq

try:
    from app.services.audit import write_audit
except Exception:  # 避免循环导入问题
    write_audit = None  # type: ignore


def _gen_init_password(length: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits + "#@$%"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def open_account(session: Session, req: OpenAccountReq) -> dict:
    """开户。返回 {id, tenant_no, admin_init_password}。"""
    # P2: 开户 status 合法性校验
    if req.status not in TenantStatus.valid_values():
        raise ParamError(message="非法的租户状态", fields={"status": req.status})
    # 唯一性：租户名 / wx_appid
    if req.wxAppid:
        exists = session.scalar(select(PfTenant).where(PfTenant.wx_appid == req.wxAppid))
        if exists:
            raise ConflictError("小程序AppID已存在")

    tenant_no = next_tenant_no(session)
    tenant = PfTenant(
        tenant_no=tenant_no,
        name=req.name,
        contact_name=req.contactName,
        contact_phone=req.contactPhone,
        qualification=req.qualification,
        status=req.status,
        expire_at=req.expireAt,
        goods_limit=req.goodsLimit,
        member_limit=req.memberLimit,
        store_limit=req.storeLimit,
        staff_limit=req.staffLimit,
        wx_appid=req.wxAppid,
        wx_secret_enc=encrypt_secret(req.wxSecret),
        remark=req.remark,
    )
    session.add(tenant)
    session.flush()  # 拿到 tenant.id

    # 功能开通：请求指定 或 取默认勾选
    codes = req.features
    if not codes:
        codes = [f.code for f in session.scalars(select(PfFeature).where(PfFeature.default_on == 1)).all()]
    for code in codes:
        session.add(PfTenantFeature(tenant_id=tenant.id, feature_code=code, enabled=1))

    # 商家管理员账号（mc_staff 为业务表，before_flush 强制注入 tenant_id）。
    # 平台端 /api/pf 默认无 tenant 上下文，此处临时注入新租户上下文，写完后恢复平台上下文，
    # 避免多租户 Fail-Fast（写入业务表缺少租户上下文 → 40100）。
    init_pwd = _gen_init_password()
    set_tenant(tenant.id)
    try:
        admin = McStaff(
            tenant_id=tenant.id,
            account=req.adminAccount,
            name=req.adminName,
            password_hash=hash_password(init_pwd),
            phone=req.adminPhone,
            role_id=0,
            is_admin=1,
            status="ENABLED",
            pwd_reset_required=1,
        )
        session.add(admin)

        if write_audit is not None:
            write_audit(
                session, action="TENANT_OPEN", target_type="pf_tenant", target_id=str(tenant.id),
                detail={"after": {"name": req.name, "tenant_no": tenant_no, "features": codes}},
                tenant_id=tenant.id,
            )
        session.flush()
    finally:
        # 恢复平台上下文（平台端 tenant_id 应为 None），防止串租户脏写。
        set_tenant(None)

    return {"id": tenant.id, "tenant_no": tenant_no, "admin_init_password": init_pwd}


def _tenant_usage_stats(session: Session, tenant_id: int) -> dict:
    """真实聚合单个租户的商品/会员/门店已用量与营收。

    口径：商品数=gd_goods(未删)、会员数=mb_member(未删)、门店数=mc_store(未删)、
    营收=od_order 已支付(pay_amount 求和，无订单返回 0)。
    平台端跨租户聚合，须用 skip_tenant_filter 逃逸 SELECT 的租户 Fail-Fast。
    """
    skip = {"skip_tenant_filter": True}
    goods_used = session.scalar(
        select(func.count(GdGoods.id)).where(
            GdGoods.tenant_id == tenant_id, GdGoods.deleted_at.is_(None)
        ).execution_options(**skip)
    ) or 0
    member_used = session.scalar(
        select(func.count(MbMember.id)).where(
            MbMember.tenant_id == tenant_id, MbMember.deleted_at.is_(None)
        ).execution_options(**skip)
    ) or 0
    store_used = session.scalar(
        select(func.count(McStore.id)).where(
            McStore.tenant_id == tenant_id, McStore.deleted_at.is_(None)
        ).execution_options(**skip)
    ) or 0
    revenue = session.scalar(
        select(func.coalesce(func.sum(OdOrder.pay_amount), 0)).where(
            OdOrder.tenant_id == tenant_id, OdOrder.status.in_(("PAID", "SHIPPED", "COMPLETED", "RECEIVED"))
        ).execution_options(**skip)
    ) or 0
    return {
        "goodsUsed": int(goods_used),
        "memberUsed": int(member_used),
        "storeUsed": int(store_used),
        "memberCount": int(member_used),
        "revenue": str(revenue),
    }


def list_tenants(
    session: Session,
    *,
    keyword: str | None,
    status: str | None,
    expire_start: str | None,
    expire_end: str | None,
    page: int,
    size: int,
    sort_by: str | None = None,
    sort_order: str = "desc",
) -> dict:
    stmt = select(PfTenant)
    if keyword:
        stmt = stmt.where(PfTenant.name.like(f"%{keyword}%"))
    if status:
        stmt = stmt.where(PfTenant.status == status)
    if expire_start:
        stmt = stmt.where(PfTenant.expire_at >= expire_start)
    if expire_end:
        stmt = stmt.where(PfTenant.expire_at <= expire_end)
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    sort_col = {"revenue": PfTenant.id, "memberCount": PfTenant.id, "createdAt": PfTenant.created_at}.get(sort_by or "createdAt", PfTenant.id)
    ordering = sort_col.asc() if sort_order.lower() == "asc" else sort_col.desc()
    rows = session.scalars(stmt.order_by(ordering).offset((page - 1) * size).limit(size)).all()
    items = []
    for t in rows:
        usage = _tenant_usage_stats(session, t.id)
        items.append(
            {
                "id": t.id,
                "tenantNo": t.tenant_no,
                "name": t.name,
                "contactName": t.contact_name,
                "contactPhone": t.contact_phone,
                "status": t.status,
                "expireAt": t.expire_at.isoformat() if t.expire_at else None,
                "openedAt": t.opened_at.isoformat() if t.opened_at else None,
                "goodsLimit": t.goods_limit,
                "memberLimit": t.member_limit,
                "staffLimit": t.staff_limit,
                "storeLimit": t.store_limit,
                **usage,
            }
        )
    return {"list": items, "total": total or 0, "page": page, "size": size}


def get_tenant_detail(session: Session, tenant_id: int) -> dict:
    t = session.get(PfTenant, tenant_id)
    if not t:
        raise NotFoundError("租户不存在")
    feature_count = session.scalar(
        select(func.count()).where(PfTenantFeature.tenant_id == tenant_id, PfTenantFeature.enabled == 1)
    ) or 0
    staff_count = session.scalar(
        select(func.count()).where(McStaff.tenant_id == tenant_id, McStaff.deleted_at.is_(None))
    ) or 0
    usage = _tenant_usage_stats(session, tenant_id)
    return {
        "id": t.id,
        "tenantNo": t.tenant_no,
        "name": t.name,
        "contactName": t.contact_name,
        "contactPhone": t.contact_phone,
        "qualification": t.qualification,
        "status": t.status,
        "expireAt": t.expire_at.isoformat() if t.expire_at else None,
        "goodsLimit": t.goods_limit,
        "memberLimit": t.member_limit,
        "storeLimit": t.store_limit,
        "staffLimit": t.staff_limit,
        "wxAppid": t.wx_appid,
        "wxAuthStatus": t.wx_auth_status,
        "permVer": t.perm_ver,
        "remark": t.remark,
        "openedAt": t.opened_at.isoformat() if t.opened_at else None,
        "createdAt": t.created_at.isoformat() if t.created_at else None,
        "featureCount": feature_count,
        "staffCount": staff_count,
        "goodsUsed": usage["goodsUsed"],
        "memberUsed": usage["memberUsed"],
        "storeUsed": usage["storeUsed"],
        "memberCount": usage["memberCount"],
        "revenue": usage["revenue"],
    }


def update_tenant(session: Session, tenant_id: int, req: UpdateTenantReq) -> None:
    t = session.get(PfTenant, tenant_id)
    if not t:
        raise NotFoundError("租户不存在")
    # P2: status 合法性校验（仅当请求显式携带 status），非法值直接业务报错，不落库。
    if getattr(req, "status", None) is not None and req.status not in TenantStatus.valid_values():
        raise ParamError(message="非法的租户状态", fields={"status": req.status})
    changed = {}
    for field, attr in [
        ("name", "name"), ("contactName", "contact_name"), ("contactPhone", "contact_phone"),
        ("qualification", "qualification"), ("status", "status"), ("expireAt", "expire_at"),
        ("goodsLimit", "goods_limit"), ("memberLimit", "member_limit"),
        ("storeLimit", "store_limit"), ("staffLimit", "staff_limit"),
        ("wxAppid", "wx_appid"), ("remark", "remark"),
    ]:
        val = getattr(req, field)
        if val is None:
            continue
        # 序列化兜底：date/datetime 不可直接写入 JSON/审计 detail 列，转 isoformat 串。
        changed[field] = val.isoformat() if isinstance(val, (date, datetime)) else val
        setattr(t, attr, val)
    # Secret 绝不进入审计 detail；只有显式传入非空新值时替换密文。
    if req.wxSecret:
        t.wx_secret_enc = encrypt_secret(req.wxSecret)
    if changed and write_audit is not None:
        write_audit(
            session, action="TENANT_QUOTA_CHANGE", target_type="pf_tenant", target_id=str(tenant_id),
            detail={"after": changed}, tenant_id=tenant_id,
        )


def set_status(session: Session, tenant_id: int, status: str) -> None:
    t = session.get(PfTenant, tenant_id)
    if not t:
        raise NotFoundError("租户不存在")
    t.status = status
    if write_audit is not None:
        write_audit(
            session, action="TENANT_STATUS_CHANGE", target_type="pf_tenant", target_id=str(tenant_id),
            detail={"after": {"status": status}}, tenant_id=tenant_id,
        )


def renew(session: Session, tenant_id: int, expire_at: str) -> None:
    t = session.get(PfTenant, tenant_id)
    if not t:
        raise NotFoundError("租户不存在")
    t.expire_at = expire_at
    if t.status in ("EXPIRED", "DISABLED"):
        t.status = "NORMAL"
    if write_audit is not None:
        write_audit(
            session, action="TENANT_RENEW", target_type="pf_tenant", target_id=str(tenant_id),
            detail={"after": {"expire_at": expire_at, "status": t.status}}, tenant_id=tenant_id,
        )


def set_features(session: Session, tenant_id: int, codes: list[str]) -> None:
    """批量设置开通功能（覆盖式）。perm_ver++ 令 Token 缓存失效。"""
    t = session.get(PfTenant, tenant_id)
    if not t:
        raise NotFoundError("租户不存在")
    session.query(PfTenantFeature).filter_by(tenant_id=tenant_id).delete()
    for code in codes:
        session.add(PfTenantFeature(tenant_id=tenant_id, feature_code=code, enabled=1))
    t.perm_ver = (t.perm_ver or 1) + 1
    if write_audit is not None:
        write_audit(
            session, action="TENANT_FEATURE_CHANGE", target_type="pf_tenant", target_id=str(tenant_id),
            detail={"after": {"features": codes, "perm_ver": t.perm_ver}}, tenant_id=tenant_id,
        )


def get_features(session: Session, tenant_id: int) -> list[str]:
    rows = session.scalars(
        select(PfTenantFeature.feature_code).where(
            PfTenantFeature.tenant_id == tenant_id, PfTenantFeature.enabled == 1
        )
    ).all()
    return list(rows)
