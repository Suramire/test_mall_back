"""用户端 P1 契约：积分商城（POINTS 通道）与商品搜索。

设计依据：docs/architecture/03-API设计.md §5.2 / §5.4。
- GET  /api/c/points-goods         积分商城列表（pure/mixed，含 limit/stock）
- GET  /api/c/points-goods/{id}    兑换详情（按钮三态：可兑/已兑完/余额不足）
- POST /api/c/points-order         ⚡积分兑换下单（channel=POINTS，order_no 前缀 PT，
                                   与普通下单共用 order 表与库存三段式）
- GET  /api/c/search?keyword=      商品搜索（商品名/副标题 LIKE，NORMAL 渠道在售）
- GET  /api/c/goods?channel=POINTS 兼容：POINTS 渠道商品列表（积分商城入口复用）

实现原则（03-API §5.4 注）：积分兑换与普通下单共用同一 order 表与库存
三段式（lock→confirm/release），仅 channel 与定价模式不同；不允许打补丁
覆写。本文件所有 handler 走 ctx(request)（SCOPE_CUSTOMER），路由挂 /c 前缀。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Query, Request

from app.api.mall import ctx
from app.core.errors import BizCode
from app.core.exceptions import BizError, ParamError
from app.core.id_generator import next_order_no
from app.core.response import err, ok, page
from app.db.session import SessionLocal
from app.models.gd_goods import GdGoods, GdSku, GdSkuStock
from app.models.mb_member import MbMember
from app.models.od_order import OdOrder, OdOrderItem
from app.services import inventory

router = APIRouter(tags=["用户端-积分商城/搜索"])

# 积分商城渠道常量（与 GdGoods.channel / GdSkuStock.channel 一致）
_POINTS_CH = "POINTS"
_NORMAL_CH = "NORMAL"
# 兑换上限：单 SKU 单次最多 99 件
_MAX_QTY = 99


def _points_goods_rows(s, tid: int, keyword: str | None, category_id: int | None):
    """积分商城在售商品（channel=POINTS，points_on_sale=1，status=ON_SALE）。"""
    from sqlalchemy import func

    q = s.query(GdGoods).filter(
        GdGoods.tenant_id == tid,
        GdGoods.deleted_at.is_(None),
        GdGoods.status == "ON_SALE",
        GdGoods.points_on_sale == 1,
    )
    if keyword:
        q = q.filter(GdGoods.name.like(f"%{keyword}%"))
    if category_id:
        q = q.filter(GdGoods.points_category_id == category_id)
    rows = q.order_by(GdGoods.sort.desc(), GdGoods.id.desc()).all()
    ids = [x.id for x in rows]
    price_map, points_map, cash_map, stock_map = {}, {}, {}, {}
    if ids:
        price_map = {g: p for g, p in s.query(GdSku.goods_id, func.min(GdSku.price)).filter(
            GdSku.tenant_id == tid, GdSku.goods_id.in_(ids), GdSku.deleted_at.is_(None)
        ).group_by(GdSku.goods_id).all()}
        points_map = {g: p for g, p in s.query(GdSku.goods_id, func.min(GdSku.points)).filter(
            GdSku.tenant_id == tid, GdSku.goods_id.in_(ids), GdSku.deleted_at.is_(None)
        ).group_by(GdSku.goods_id).all()}
        cash_map = {g: c for g, c in s.query(GdSku.goods_id, func.min(GdSku.cash)).filter(
            GdSku.tenant_id == tid, GdSku.goods_id.in_(ids), GdSku.deleted_at.is_(None)
        ).group_by(GdSku.goods_id).all()}
        stock_map = {g: c for g, c in s.query(GdSkuStock.goods_id, func.coalesce(func.sum(GdSkuStock.available_stock), 0)).filter(
            GdSkuStock.tenant_id == tid, GdSkuStock.goods_id.in_(ids), GdSkuStock.channel == _POINTS_CH
        ).group_by(GdSkuStock.goods_id).all()}
    out = []
    for x in rows:
        sku_points = int(points_map.get(x.id) or 0)
        cash = cash_map.get(x.id) or Decimal("0.00")
        out.append({
            "id": x.id, "name": x.name, "subtitle": x.subtitle,
            "mainImage": x.main_image, "type": x.type, "channel": x.channel,
            "status": x.status, "price": str(price_map.get(x.id) or "0.00"),
            "points": sku_points, "cash": str(cash),
            "priceMode": "POINTS" if cash == 0 else "MIXED",
            "stock": int(stock_map.get(x.id) or 0), "soldCount": x.sold_count,
            "limit": x.points_limit_per_user or 0,
        })
    return out


def _points_sku(s, tid: int, sku_id: int):
    """积分商城 SKU 校验：SKU 存在、所属商品 POINTS 渠道且在售。"""
    sku = s.query(GdSku).filter_by(id=sku_id, tenant_id=tid, deleted_at=None).first()
    if not sku:
        raise BizError(BizCode.ORDER_SKU_INVALID, "SKU不存在")
    g = s.query(GdGoods).filter_by(id=sku.goods_id, tenant_id=tid, deleted_at=None).first()
    if not g or g.status != "ON_SALE" or not g.points_on_sale:
        raise BizError(BizCode.ORDER_GOODS_OFF_SALE, "商品已失效")
    return sku, g


@router.get("/points-goods")
def points_goods_list(request: Request, keyword: str | None = None,
                      categoryId: int | None = None,
                      page_no: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
    tid, _ = ctx(request)
    with SessionLocal() as s:
        rows = _points_goods_rows(s, tid, keyword, categoryId)
        total = len(rows)
        slice_rows = rows[(page_no - 1) * size: page_no * size]
        return page(slice_rows, total, page_no, size)


@router.get("/points-goods/{goods_id}")
def points_goods_detail(goods_id: int, request: Request):
    tid, mid = ctx(request)
    with SessionLocal() as s:
        g = s.query(GdGoods).filter_by(id=goods_id, tenant_id=tid, deleted_at=None).first()
        if not g or g.status != "ON_SALE" or not g.points_on_sale:
            return err(BizCode.NOT_FOUND, "商品不存在或已下架")
        skus = s.query(GdSku).filter_by(goods_id=g.id, tenant_id=tid, deleted_at=None).all()
        stocks = s.query(GdSkuStock).filter_by(tenant_id=tid, goods_id=g.id, channel=_POINTS_CH).all()
        member = s.query(MbMember).filter_by(id=mid, tenant_id=tid).first()
        balance = member.points_balance if member else 0
        total_stock = sum((x.available_stock or 0) for x in stocks)
        out_skus = []
        for sku in skus:
            sku_stock = sum((x.available_stock or 0) for x in stocks if x.sku_id == sku.id)
            out_skus.append({
                "id": sku.id, "skuCode": sku.sku_code, "specText": sku.spec_text,
                "specJson": sku.spec_json or {}, "price": str(sku.price),
                "originalPrice": str(sku.original_price), "points": sku.points,
                "cash": str(sku.cash), "stock": sku_stock,
                "available": sku_stock > 0,
            })
        # 按钮三态：可兑 / 已兑完 / 余额不足
        affordable = any(sku.points <= balance and sku_stock > 0
                         for sku, sku_stock in
                         ((x, sum((z.available_stock or 0) for z in stocks if z.sku_id == x.id)) for x in skus))
        sold_out = total_stock <= 0
        state = "SOLD_OUT" if sold_out else ("AFFORDABLE" if affordable else "INSUFFICIENT")
        return ok({
            "id": g.id, "name": g.name, "subtitle": g.subtitle, "detail": g.detail,
            "mainImage": g.main_image, "images": g.images or [], "type": g.type,
            "channel": g.channel, "status": g.status, "pointsBalance": balance,
            "totalStock": total_stock, "limit": g.points_limit_per_user or 0,
            "buttonState": state, "price": str(skus[0].price if skus else "0.00"),
            "skus": out_skus,
        })


@router.post("/points-order")
def points_order_create(payload: dict, request: Request):
    """⚡积分兑换下单：channel=POINTS，order_no 前缀 PT，纯积分/积分+补差现金。

    定价模式：
    - 纯积分单：sku.points 全额抵扣，cash=0 → pay_amount=0，pay_points=points*qty
    - 混合单：   points 抵扣 + cash 补差 → pay_amount=cash*qty，pay_points=points*qty
    库存走 POINTS 渠道三段式（lock_stock），支付动作由 /order/{id}/pay 承接。
    """
    tid, mid = ctx(request)
    idem = (request.headers.get("Idempotency-Key") or "").strip()
    if len(idem) > 80:
        raise ParamError({"Idempotency-Key": "长度不能超过80"})
    try:
        sku_id = int(payload.get("skuId") or payload.get("sku_id") or 0)
        quantity = int(payload.get("quantity") or 1)
    except (TypeError, ValueError):
        raise ParamError({"skuId": "必须是整数", "quantity": "必须是整数"})
    if quantity < 1 or quantity > _MAX_QTY:
        raise ParamError({"quantity": f"数量须为 1-{_MAX_QTY}"})
    with SessionLocal() as s:
        if idem:
            old = s.query(OdOrder).filter_by(tenant_id=tid, member_id=mid, idempotency_key=idem).first()
            if old:
                return ok({"id": old.id, "orderNo": old.order_no,
                           "payAmount": str(old.pay_amount), "payPoints": old.pay_points,
                           "status": old.status})
        sku, g = _points_sku(s, tid, sku_id)
        stock = s.query(GdSkuStock).filter_by(tenant_id=tid, sku_id=sku.id, channel=_POINTS_CH).first()
        if not stock or stock.available_stock < quantity:
            raise BizError(BizCode.STOCK_NOT_ENOUGH, "库存不足")
        member = s.query(MbMember).filter_by(id=mid, tenant_id=tid).first()
        if not member:
            raise BizError(BizCode.NOT_FOUND, "会员不存在")
        need_points = int(sku.points or 0) * quantity
        if member.points_balance < need_points:
            raise BizError(BizCode.POINTS_NOT_ENOUGH, "积分余额不足")
        cash = (sku.cash or Decimal("0.00")) * quantity
        no = next_order_no(s, tid, prefix="PT")
        o = OdOrder(
            tenant_id=tid, order_no=no, channel=_POINTS_CH, member_id=mid,
            idempotency_key=idem or None, status="PENDING_PAY",
            delivery_type=str(payload.get("deliveryType") or "EXPRESS"),
            goods_amount=cash, pay_amount=cash, pay_points=need_points,
            receiver_name=str(payload.get("receiverName") or payload.get("receiver_name") or ""),
            receiver_phone=str(payload.get("receiverPhone") or payload.get("receiver_phone") or ""),
            receiver_address=str(payload.get("receiverAddress") or payload.get("receiver_address") or ""),
            pay_deadline=datetime.now(UTC) + timedelta(minutes=30),
        )
        s.add(o)
        s.flush()
        inventory.lock_stock(s, [{"skuId": sku.id, "channel": _POINTS_CH, "qty": quantity}], no)
        s.add(OdOrderItem(
            tenant_id=tid, order_id=o.id, goods_id=g.id, sku_id=sku.id,
            channel=_POINTS_CH, goods_name=g.name, goods_type=g.type,
            spec_text=sku.spec_text, image=sku.image, price=sku.price,
            quantity=quantity, subtotal_amount=cash,
        ))
        s.commit()
        return ok({"id": o.id, "orderNo": no, "payAmount": str(cash),
                   "payPoints": need_points, "status": o.status})


@router.get("/search")
def customer_search(request: Request, keyword: str = Query(..., min_length=1, max_length=50),
                    page_no: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
    """商品搜索（03-API §5.2 P0）：NORMAL 渠道在售商品，商品名/副标题 LIKE。"""
    tid, _ = ctx(request)
    from sqlalchemy import func

    with SessionLocal() as s:
        q = s.query(GdGoods).filter(
            GdGoods.tenant_id == tid, GdGoods.deleted_at.is_(None),
            GdGoods.status == "ON_SALE", GdGoods.normal_on_sale == 1,
        ).filter(GdGoods.name.like(f"%{keyword}%"))
        total = q.count()
        rows = q.order_by(GdGoods.sort.desc(), GdGoods.id.desc()).offset((page_no - 1) * size).limit(size).all()
        ids = [x.id for x in rows]
        price_map, stock_map = {}, {}
        if ids:
            price_map = {g: p for g, p in s.query(GdSku.goods_id, func.min(GdSku.price)).filter(
                GdSku.tenant_id == tid, GdSku.goods_id.in_(ids), GdSku.deleted_at.is_(None)
            ).group_by(GdSku.goods_id).all()}
            stock_map = {g: c for g, c in s.query(GdSkuStock.goods_id, func.coalesce(func.sum(GdSkuStock.available_stock), 0)).filter(
                GdSkuStock.tenant_id == tid, GdSkuStock.goods_id.in_(ids), GdSkuStock.channel == _NORMAL_CH
            ).group_by(GdSkuStock.goods_id).all()}
        return page([{
            "id": x.id, "name": x.name, "subtitle": x.subtitle, "mainImage": x.main_image,
            "type": x.type, "channel": x.channel, "status": x.status,
            "price": str(price_map.get(x.id) or "0.00"),
            "stock": int(stock_map.get(x.id) or 0), "soldCount": x.sold_count,
        } for x in rows], total, page_no, size)


@router.get("/goods")
def customer_goods_channel_list(request: Request, channel: str | None = None,
                                keyword: str | None = None, categoryId: int | None = None,
                                page_no: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
    """商品列表兼容端点：channel=POINTS 返回积分商城列表。

    注：/c/goods 已由 shop_router 注册在前（NORMAL 渠道），本端点仅服务
    channel=POINTS 的显式请求（若被注册顺序覆盖则不可达，积分商城入口
    一律走 /c/points-goods）。
    """
    tid, _ = ctx(request)
    if channel == _POINTS_CH:
        with SessionLocal() as s:
            rows = _points_goods_rows(s, tid, keyword, categoryId)
            total = len(rows)
            return page(rows[(page_no - 1) * size: page_no * size], total, page_no, size)
    # 非 POINTS 通道直接复用 mall.py 的 shop_goods_list 语义（避免重复实现）
    from app.api.mall import shop_goods_list
    return shop_goods_list(request, page_no=page_no, size=size, keyword=keyword, categoryId=categoryId)
