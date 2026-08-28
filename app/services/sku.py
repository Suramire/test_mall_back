"""SKU 服务：SKU 创建/更新、价格 Decimal 校验、三模式定价（CASH/POINTS/MIXED）。

口径：docs/architecture/02-数据库设计.md §3 `gd_sku`、03-API设计.md §3.3。
- 金额一律 Decimal 落库（Numeric(10,2)），禁止 float 直传。
- sku_code 规则：SKU + 商品ID + 3位序号（DDL 注释口径），请求未指定时自动生成。
- SKU 与渠道库存（gd_sku_stock）成对维护：每个 SKU 可带 NORMAL/POINTS 两条初始库存。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ParamError
from app.core.tenant_context import require_tenant_id
from app.models.gd_goods import GdSku, GdSkuStock
from app.services import inventory

PRICE_MODES = ("CASH", "POINTS", "MIXED")
_TWO_PLACES = Decimal("0.01")


def to_decimal(value, field: str) -> Decimal:
    """str/int/float → Decimal(10,2)。非法值抛 40001。"""
    if value is None:
        return Decimal("0.00")
    try:
        d = Decimal(str(value)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise ParamError(fields={field: "金额格式非法"}, message="金额格式非法")
    if d < 0:
        raise ParamError(fields={field: "金额不得为负"}, message="金额格式非法")
    return d


def gen_sku_code(session: Session, goods_id: int, seq: int) -> str:
    """SKU + 商品ID + 3位序号（DDL 注释口径，如 SKU10086001）。"""
    return f"SKU{goods_id}{seq:03d}"


def validate_sku_payload(sku: dict, channel: str) -> None:
    """定价模式与渠道一致性校验（40001）。"""
    mode = str(sku.get("priceMode") or "CASH")
    if mode not in PRICE_MODES:
        raise ParamError(fields={"priceMode": "仅支持 CASH/POINTS/MIXED"})
    points = int(sku.get("points") or 0)
    price = to_decimal(sku.get("price"), "price")
    if mode == "CASH" and price <= 0:
        raise ParamError(fields={"price": "现金模式售价必须大于 0"})
    if mode in ("POINTS", "MIXED") and points <= 0:
        raise ParamError(fields={"points": "积分模式所需积分必须大于 0"})
    # 渠道一致性：POINTS 渠道商品必须存在积分定价 SKU
    if channel == "POINTS" and mode == "CASH":
        raise ParamError(
            fields={"priceMode": "积分商城商品的 SKU 需为 POINTS/MIXED 定价"}
        )


def _apply_sku_fields(sku: GdSku, payload: dict) -> None:
    """把请求字段写入 SKU 实体（金额统一 Decimal）。"""
    if "specJson" in payload or "spec_json" in payload:
        sku.spec_json = payload.get("specJson", payload.get("spec_json"))
    if "specText" in payload or "spec_text" in payload:
        sku.spec_text = str(payload.get("specText", payload.get("spec_text", "")))
    if "image" in payload:
        sku.image = str(payload.get("image") or "")
    if "price" in payload:
        sku.price = to_decimal(payload.get("price"), "price")
    if "originalPrice" in payload or "original_price" in payload:
        sku.original_price = to_decimal(
            payload.get("originalPrice", payload.get("original_price")),
            "originalPrice",
        )
    if "priceMode" in payload or "price_mode" in payload:
        mode = str(payload.get("priceMode", payload.get("price_mode", "CASH")))
        if mode not in PRICE_MODES:
            raise ParamError(fields={"priceMode": "仅支持 CASH/POINTS/MIXED"})
        sku.price_mode = mode
    if "points" in payload:
        sku.points = int(payload.get("points") or 0)
    if "cash" in payload:
        sku.cash = to_decimal(payload.get("cash"), "cash")
    if "weight" in payload:
        try:
            sku.weight = Decimal(str(payload.get("weight") or 0)).quantize(
                Decimal("0.001")
            )
        except InvalidOperation:
            raise ParamError(fields={"weight": "重量格式非法"})


def create_skus(session: Session, goods_id: int, skus: list[dict], channel: str) -> list[GdSku]:
    """创建 SKU + 渠道初始库存。返回已 flush 的 SKU 列表。"""
    tid = require_tenant_id()
    if not skus:
        return []
    created: list[GdSku] = []
    for seq, payload in enumerate(skus, start=1):
        validate_sku_payload(payload, channel)
        sku = GdSku(
            tenant_id=tid,
            goods_id=goods_id,
            sku_code=str(payload.get("skuCode") or "")
            or gen_sku_code(session, goods_id, seq),
        )
        _apply_sku_fields(sku, payload)
        session.add(sku)
        session.flush()  # 拿到 sku.id 再落库存
        stock_payload = list(payload.get("stocks") or [])
        # 兼容商品表单旧契约：normalStock/pointsStock/stock
        if not stock_payload:
            if "normalStock" in payload:
                stock_payload.append({"channel": "NORMAL", "totalStock": payload.get("normalStock")})
            if "pointsStock" in payload:
                stock_payload.append({"channel": "POINTS", "totalStock": payload.get("pointsStock")})
            if "stock" in payload:
                stock_payload.append({"channel": "NORMAL", "totalStock": payload.get("stock")})
        for st in stock_payload:
            ch = str(st.get("channel") or "NORMAL")
            inventory.ensure_stock_row(
                session,
                goods_id,
                sku.id,
                ch,
                total_stock=int(st.get("totalStock") or 0),
                warn_stock=int(st.get("warnStock") or 0),
            )
        created.append(sku)
    return created


def update_skus(session: Session, goods_id: int, skus: list[dict], channel: str) -> list[GdSku]:
    """编辑 SKU：带 id → 更新；不带 id → 追加创建。返回受影响 SKU。"""
    tid = require_tenant_id()
    touched: list[GdSku] = []
    to_create = [p for p in skus if not p.get("id")]
    for payload in (p for p in skus if p.get("id")):
        sku = session.scalar(
            select(GdSku).where(
                GdSku.tenant_id == tid,
                GdSku.id == int(payload["id"]),
                GdSku.goods_id == goods_id,
                GdSku.deleted_at.is_(None),
            )
        )
        if sku is None:
            raise NotFoundError(f"SKU {payload['id']} 不存在")
        merged = {**dict_from_sku(sku), **{k: v for k, v in payload.items() if v is not None}}
        validate_sku_payload(merged, channel)
        _apply_sku_fields(sku, payload)
        # 库存随编辑可重置 total（SET 语义，占用校验交给 inventory.adjust_stock）
        for st in payload.get("stocks") or []:
            ch = str(st.get("channel") or "NORMAL")
            row = inventory.ensure_stock_row(session, goods_id, sku.id, ch)
            if "totalStock" in st:
                inventory.adjust_stock(
                    session,
                    goods_id,
                    [{"skuId": sku.id, "channel": ch, "changeType": "SET",
                      "value": int(st.get("totalStock") or 0)}],
                )
            if "warnStock" in st:
                row.warn_stock = int(st.get("warnStock") or 0)
        touched.append(sku)
    if to_create:
        base_seq = (
            session.scalar(
                select(func.count(GdSku.id)).where(
                    GdSku.tenant_id == tid, GdSku.goods_id == goods_id
                )
            )
            or 0
        )
        for i, payload in enumerate(to_create):
            payload.setdefault("skuCode", "")
            if not payload["skuCode"]:
                payload["skuCode"] = gen_sku_code(session, goods_id, base_seq + i + 1)
        touched.extend(create_skus(session, goods_id, to_create, channel))
    return touched


def dict_from_sku(sku: GdSku) -> dict:
    return {
        "id": sku.id,
        "skuCode": sku.sku_code,
        "specJson": sku.spec_json or {},
        "specText": sku.spec_text,
        "image": sku.image,
        "price": sku.price,
        "originalPrice": sku.original_price,
        "priceMode": sku.price_mode,
        "points": sku.points,
        "cash": sku.cash,
        "weight": sku.weight,
    }


def serialize_sku(sku: GdSku, stock_rows: list[GdSkuStock]) -> dict:
    """详情序列化：SKU 字段 + 双渠道库存。金额转字符串保精度。"""
    data = dict_from_sku(sku)
    data["price"] = str(sku.price)
    data["originalPrice"] = str(sku.original_price)
    data["cash"] = str(sku.cash)
    data["weight"] = str(sku.weight)
    data["stocks"] = [
        {
            "channel": r.channel,
            "totalStock": r.total_stock,
            "lockedStock": r.locked_stock,
            "soldStock": r.sold_stock,
            "availableStock": r.available_stock,
            "warnStock": r.warn_stock,
        }
        for r in stock_rows
        if r.sku_id == sku.id
    ]
    return data


def min_price_of_goods(session: Session, goods_id: int) -> Decimal | None:
    """列表页展示价：最低 SKU 售价。"""
    tid = require_tenant_id()
    return session.scalar(
        select(func.min(GdSku.price)).where(
            GdSku.tenant_id == tid,
            GdSku.goods_id == goods_id,
            GdSku.deleted_at.is_(None),
        )
    )
