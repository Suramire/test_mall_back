"""商品（SPU）服务：创建/编辑/查询/软删 + 双渠道（NORMAL/POINTS）独立上下架。

口径：docs/architecture/02-数据库设计.md §3（gd_goods 双渠道开关）、
03-API设计.md §3.3（41003 配额 / 42004 信息不全 / 错误码）。

要点：
- 上下架按渠道独立（R-CH-04）：normal_on_sale / points_on_sale 两个开关，
  status 仅表达整体生命周期 DRAFT/ON_SALE/OFF_SALE。
- SOLD_OUT 为派生状态不落库（02 §公共.6.3）：列表接口计算透出。
- 创建时校验租户商品配额（pf_tenant.goods_limit，0=不限），超限 41003。
- 上架前做信息完备性检查，缺失返回 42004。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import BizCode
from app.core.exceptions import BizError, NotFoundError, ParamError
from app.core.tenant_context import require_tenant_id
from app.models.gd_goods import GdGoods, GdSku
from app.models.pf_tenant import PfTenant
from app.models.sys_common import SysFile
from app.services import inventory
from app.services import sku as sku_svc

GOODS_TYPES = ("PHYSICAL", "VIRTUAL", "TICKET")
CHANNELS = ("NORMAL", "POINTS", "BOTH")


class GoodsQuotaError(BizError):
    """41003 商品配额超限。"""

    def __init__(self, message: str = "商品数量已达套餐配额上限") -> None:
        super().__init__(code=BizCode.TENANT_QUOTA_GOODS, message=message)


class ShelfInfoIncompleteError(BizError):
    """42004 商品信息不全，无法上架。"""

    def __init__(self, missing: list[str]) -> None:
        super().__init__(
            code=BizCode.GOODS_SHELF_INFO_INCOMPLETE,
            message="商品信息不全，无法上架：" + "、".join(missing),
            data={"missing": missing},
        )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _get_goods(session: Session, goods_id: int) -> GdGoods:
    """按租户取商品；租户隔离由 ORM 钩子 + 显式条件双重保证。"""
    tid = require_tenant_id()
    goods = session.scalar(
        select(GdGoods).where(
            GdGoods.tenant_id == tid,
            GdGoods.id == goods_id,
            GdGoods.deleted_at.is_(None),
        )
    )
    if goods is None:
        raise NotFoundError("商品不存在")
    return goods


def _check_quota(session: Session) -> None:
    """商品配额：pf_tenant.goods_limit（平台表，无租户注入；0=不限）。"""
    tid = require_tenant_id()
    tenant = session.get(PfTenant, tid)
    limit = tenant.goods_limit if tenant is not None else 0
    if not limit:
        return
    count = session.scalar(
        select(func.count(GdGoods.id)).where(
            GdGoods.tenant_id == tid, GdGoods.deleted_at.is_(None)
        )
    )
    if (count or 0) >= limit:
        raise GoodsQuotaError()


def _apply_goods_fields(session: Session, g: GdGoods, payload: dict) -> None:
    """请求字段 → SPU 实体。只处理 SPU 级字段（SKU/库存交给 sku_svc）。"""
    mapping = {
        "name": "name",
        "subtitle": "subtitle",
        "type": "type",
        "channel": "channel",
        "normalCategoryId": "normal_category_id",
        "pointsCategoryId": "points_category_id",
        "mainImage": "main_image",
        "images": "images",
        "detail": "detail",
        "specConfig": "spec_config",
        "freightTemplateId": "freight_template_id",
        "sort": "sort",
        "virtualSold": "virtual_sold",
        "virtualDesc": "virtual_desc",
        "pointsLimitPerUser": "points_limit_per_user",
        "pointsLimitPerDay": "points_limit_per_day",
    }
    refs = [payload.get("mainImage"), *(payload.get("images") or [])]
    for ref in refs:
        if isinstance(ref, str) and ref.startswith("/api/common/upload/file/"):
            stored = ref.rsplit("/", 1)[-1]
            if not session.scalar(select(SysFile).where(SysFile.tenant_id == require_tenant_id(), SysFile.url == ref)):
                raise ParamError(fields={"image": f"上传文件不存在或不属于当前租户: {stored}"})
    for key, attr in mapping.items():
        if key in payload and payload[key] is not None:
            setattr(g, attr, payload[key])
    if "type" in payload and payload["type"] not in GOODS_TYPES:
        # 兼容既有种子/历史数据的字面量（如 NORMAL），仅对新值做枚举约束外的宽容
        pass
    if "channel" in payload and payload["channel"] not in CHANNELS:
        raise ParamError(fields={"channel": "仅支持 NORMAL/POINTS/BOTH"})
    # 核销券配置（type=TICKET）
    ticket = payload.get("ticketConfig")
    if ticket:
        if ticket.get("validType") not in ("FIXED_DATE", "DAYS_AFTER_PAY"):
            raise ParamError(
                fields={"ticketConfig.validType": "仅支持 FIXED_DATE/DAYS_AFTER_PAY"}
            )
        g.valid_type = ticket.get("validType")
        g.valid_end_date = ticket.get("validEndDate")
        g.valid_days = ticket.get("validDays")
        g.verify_store_ids = ticket.get("verifyStoreIds")
        g.verify_desc = ticket.get("verifyDesc") or ""
        g.expire_refund_policy = ticket.get("expireRefundPolicy")


def _ticket_config_of(g: GdGoods) -> dict | None:
    if g.type != "TICKET" and not g.valid_type:
        return None
    return {
        "validType": g.valid_type,
        "validEndDate": g.valid_end_date.isoformat() if g.valid_end_date else None,
        "validDays": g.valid_days,
        "verifyStoreIds": g.verify_store_ids,
        "verifyDesc": g.verify_desc,
        "expireRefundPolicy": g.expire_refund_policy,
    }


def _shelf_missing(session: Session, g: GdGoods, channel: str) -> list[str]:
    """上架信息完备性检查（42004）：返回缺失项清单。

    规则（渠道级）：
    - 必须有未删除 SKU；
    - NORMAL 渠道：至少一个 SKU 现金售价 > 0；
    - POINTS 渠道：至少一个 SKU 为 POINTS/MIXED 且积分 > 0；
    - TICKET 商品：必须配置有效期（valid_type + 结束日期/天数）。
    """
    tid = require_tenant_id()
    missing: list[str] = []
    skus = list(
        session.scalars(
            select(GdSku).where(
                GdSku.tenant_id == tid,
                GdSku.goods_id == g.id,
                GdSku.deleted_at.is_(None),
            )
        ).all()
    )
    if not skus:
        missing.append("至少一个SKU")
    if channel == "NORMAL" and not any((k.price or 0) > 0 for k in skus):
        missing.append("现金售价")
    if channel == "POINTS" and not any(
        k.price_mode in ("POINTS", "MIXED") and (k.points or 0) > 0 for k in skus
    ):
        missing.append("积分定价")
    if g.type == "TICKET" and (
        not g.valid_type or (not g.valid_end_date and not g.valid_days)
    ):
        missing.append("核销券有效期配置")
    return missing


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def create_goods(session: Session, payload: dict) -> dict:
    """新增商品（含 SKU 数组、双渠道库存、核销券配置）。配额校验 41003。"""
    tid = require_tenant_id()
    _check_quota(session)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ParamError(fields={"name": "商品名称必填"})
    g = GdGoods(
        tenant_id=tid,
        name=name,
        status=str(payload.get("status") or "DRAFT"),
        has_sku=1 if payload.get("skus") else 0,
    )
    _apply_goods_fields(session, g, payload)
    session.add(g)
    session.flush()  # 拿到 g.id
    skus = sku_svc.create_skus(
        session, g.id, payload.get("skus") or [], g.channel
    )
    if skus:
        g.has_sku = 1
    session.commit()
    return {"id": g.id, "name": g.name, "status": g.status}


def update_goods(session: Session, goods_id: int, payload: dict) -> dict:
    """编辑商品；skus 数组存在时做增改（带 id 更新 / 无 id 追加）。"""
    g = _get_goods(session, goods_id)
    _apply_goods_fields(session, g, payload)
    if "skus" in payload and payload["skus"] is not None:
        sku_svc.update_skus(session, g.id, payload["skus"], g.channel)
        g.has_sku = 1
    session.commit()
    return {"id": g.id, "name": g.name, "status": g.status}


def delete_goods(session: Session, goods_id: int) -> None:
    """软删（P1）：deleted_at 置位；已上架商品先下架，防止前台残留。"""
    g = _get_goods(session, goods_id)
    g.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    g.normal_on_sale = 0
    g.points_on_sale = 0
    g.status = "OFF_SALE"
    session.commit()


def shelf_goods(session: Session, goods_id: int, channel: str, on_sale: bool) -> dict:
    """★按渠道独立上下架（R-CH-04）。信息不全上架 → 42004。"""
    if channel not in CHANNELS:
        raise ParamError(fields={"channel": "仅支持 NORMAL/POINTS/BOTH"})
    g = _get_goods(session, goods_id)
    targets = ("NORMAL", "POINTS") if channel == "BOTH" else (channel,)
    if on_sale:
        for ch in targets:
            missing = _shelf_missing(session, g, ch)
            if missing:
                raise ShelfInfoIncompleteError(missing)
    for ch in targets:
        if ch == "NORMAL":
            g.normal_on_sale = 1 if on_sale else 0
        else:
            g.points_on_sale = 1 if on_sale else 0
    # status 表达整体生命周期：任一渠道在售即 ON_SALE
    if g.normal_on_sale or g.points_on_sale:
        g.status = "ON_SALE"
    elif g.status == "ON_SALE":
        g.status = "OFF_SALE"
    session.commit()
    return {
        "id": g.id,
        "status": g.status,
        "normalOnSale": g.normal_on_sale,
        "pointsOnSale": g.points_on_sale,
    }


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def _serialize_goods_detail(session: Session, g: GdGoods) -> dict:
    tid = require_tenant_id()
    skus = list(
        session.scalars(
            select(GdSku).where(
                GdSku.tenant_id == tid,
                GdSku.goods_id == g.id,
                GdSku.deleted_at.is_(None),
            )
        ).all()
    )
    stocks = inventory.list_stocks(session, g.id)
    return {
        "id": g.id,
        "name": g.name,
        "subtitle": g.subtitle,
        "type": g.type,
        "channel": g.channel,
        "status": g.status,
        "normalOnSale": g.normal_on_sale,
        "pointsOnSale": g.points_on_sale,
        "normalCategoryId": g.normal_category_id,
        "pointsCategoryId": g.points_category_id,
        "mainImage": g.main_image,
        "images": g.images,
        "detail": g.detail,
        "hasSku": bool(g.has_sku),
        "specConfig": g.spec_config,
        "freightTemplateId": g.freight_template_id,
        "sort": g.sort,
        "soldCount": g.sold_count,
        "virtualSold": g.virtual_sold,
        "pointsLimitPerUser": g.points_limit_per_user,
        "pointsLimitPerDay": g.points_limit_per_day,
        "totalStock": sum((s.available_stock or 0) for s in stocks),
        "ticketConfig": _ticket_config_of(g),
        "skus": [sku_svc.serialize_sku(s, stocks) for s in skus],
        "createdAt": g.created_at.isoformat() if g.created_at else None,
    }


def get_goods(session: Session, goods_id: int) -> dict:
    """详情：SPU + skus[] + 双渠道库存 + ticketConfig（03-API设计.md §3.3）。"""
    return _serialize_goods_detail(session, _get_goods(session, goods_id))


def list_goods(
    session: Session,
    *,
    keyword: str | None = None,
    type_: str | None = None,
    channel: str | None = None,
    status: str | None = None,
    category_id: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict], int]:
    """商家端列表：可见全部状态（含草稿/下架）；库存列=两渠道 available 之和。"""
    tid = require_tenant_id()
    q = select(GdGoods).where(
        GdGoods.tenant_id == tid, GdGoods.deleted_at.is_(None)
    )
    if keyword:
        like = f"%{keyword}%"
        q = q.where(or_(GdGoods.name.like(like), GdGoods.subtitle.like(like)))
    if type_:
        q = q.where(GdGoods.type == type_)
    if channel == "NORMAL":
        q = q.where(GdGoods.channel.in_(("NORMAL", "BOTH")))
    elif channel == "POINTS":
        q = q.where(GdGoods.channel.in_(("POINTS", "BOTH")))
    elif channel == "BOTH":
        q = q.where(GdGoods.channel == "BOTH")
    if status:
        q = q.where(GdGoods.status == status)
    if category_id:
        q = q.where(
            or_(
                GdGoods.normal_category_id == category_id,
                GdGoods.points_category_id == category_id,
            )
        )
    total = session.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = list(
        session.scalars(
            q.order_by(GdGoods.sort.desc(), GdGoods.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        ).all()
    )
    ids = [g.id for g in rows]
    stock_map = inventory.sum_available_by_goods(session, ids)
    price_map: dict[int, object] = {}
    if ids:
        price_map = dict(
            session.execute(
                select(GdSku.goods_id, func.min(GdSku.price)).where(
                    GdSku.tenant_id == tid,
                    GdSku.goods_id.in_(ids),
                    GdSku.deleted_at.is_(None),
                )
                .group_by(GdSku.goods_id)
            ).all()
        )
    out = []
    for g in rows:
        stock = int(stock_map.get(g.id) or 0)
        # SOLD_OUT 为派生状态不落库（02 §公共.6.3）
        derived = g.status
        if g.status == "ON_SALE" and stock == 0:
            derived = "SOLD_OUT"
        out.append(
            {
                "id": g.id,
                "name": g.name,
                "subtitle": g.subtitle,
                "mainImage": g.main_image,
                "type": g.type,
                "channel": g.channel,
                "status": g.status,
                "derivedStatus": derived,
                "normalOnSale": g.normal_on_sale,
                "pointsOnSale": g.points_on_sale,
                "price": str(price_map.get(g.id) or "0.00"),
                "stock": stock,
                "soldCount": g.sold_count,
                "sort": g.sort,
                "createdAt": g.created_at.isoformat() if g.created_at else None,
            }
        )
    return out, int(total)


def stock_log_of_goods(
    session: Session, goods_id: int, page: int = 1, size: int = 50
) -> tuple[list[dict], int]:
    """库存变更日志（P1）。"""
    from app.models.gd_goods import GdStockLog

    tid = require_tenant_id()
    base = select(GdStockLog).where(
        GdStockLog.tenant_id == tid, GdStockLog.goods_id == goods_id
    )
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = session.scalars(
        base.order_by(GdStockLog.id.desc()).offset((page - 1) * size).limit(size)
    ).all()
    return (
        [
            {
                "id": x.id,
                "skuId": x.sku_id,
                "channel": x.channel,
                "changeType": x.change_type,
                "before": x.before_val,
                "change": x.change_val,
                "after": x.after_val,
                "refType": x.ref_type,
                "refId": x.ref_id,
                "operator": x.operator_name,
                "remark": x.remark,
                "createdAt": x.created_at.isoformat() if x.created_at else None,
            }
            for x in rows
        ],
        int(total),
    )
