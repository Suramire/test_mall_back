"""用户下单安全专项回归：金额篡改、下架商品、超库存、跨租户、幂等键与库存锁定。"""
from __future__ import annotations

import uuid
from decimal import Decimal

from app.core.errors import BizCode
from app.core.security import SCOPE_CUSTOMER, create_access_token
from app.core.tenant_context import reset, set_tenant
from app.models.gd_goods import GdGoods, GdSku, GdSkuStock
from app.models.mb_member import MbMember
from app.models.od_order import OdOrder, OdOrderItem
from tests.conftest import assert_biz_code

TENANT_A = 1001
TENANT_B = 2002


def _enable_sqlite_autoincrement() -> None:
    from sqlalchemy import BigInteger, Integer
    from app.db.base import Base
    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if isinstance(col.type, BigInteger):
                col.type = BigInteger().with_variant(Integer(), "sqlite")


_enable_sqlite_autoincrement()


def _customer(member_id: int, tenant_id: int) -> dict[str, str]:
    return {"Authorization": "Bearer " + create_access_token(str(member_id), SCOPE_CUSTOMER, tenant_id=tenant_id)}


def _base() -> int:
    return 80_000_000 + int(uuid.uuid4().int % 10_000_000)


def _seed_member(db, tenant_id: int, member_id: int) -> None:
    set_tenant(tenant_id)
    try:
        db.add(MbMember(id=member_id, tenant_id=tenant_id, member_no=f"QAO{member_id}", nickname="下单用户"))
        db.commit()
    finally:
        reset()


def _seed_goods(db, tenant_id: int, base: int, *, status: str = "ON_SALE", normal_on_sale: int = 1,
                total: int = 10, price: str = "12.50") -> dict:
    goods_id, sku_id = base + 1, base + 2
    set_tenant(tenant_id)
    try:
        db.add_all([
            GdGoods(id=goods_id, tenant_id=tenant_id, name=f"QA下单商品{base}", type="NORMAL",
                    channel="NORMAL", status=status, normal_on_sale=normal_on_sale),
            GdSku(id=sku_id, tenant_id=tenant_id, goods_id=goods_id, sku_code=f"QA-ORD-{sku_id}",
                  price=price, original_price="20.00"),
            GdSkuStock(tenant_id=tenant_id, goods_id=goods_id, sku_id=sku_id, channel="NORMAL",
                       total_stock=total, available_stock=total),
        ])
        db.commit()
    finally:
        reset()
    return {"goodsId": goods_id, "skuId": sku_id, "price": price, "total": total}


def _stock(db, tenant_id: int, sku_id: int) -> tuple[int, int, int, int]:
    db.expire_all()
    set_tenant(tenant_id)
    try:
        row = db.query(GdSkuStock).filter_by(tenant_id=tenant_id, sku_id=sku_id, channel="NORMAL").one()
        return row.total_stock, row.locked_stock, row.sold_stock, row.available_stock
    finally:
        reset()


def _order_count(db, tenant_id: int, member_id: int) -> int:
    db.expire_all()
    set_tenant(tenant_id)
    try:
        return db.query(OdOrder).filter_by(tenant_id=tenant_id, member_id=member_id).count()
    finally:
        reset()


def _order_amount(db, tenant_id: int, order_id: int) -> tuple[Decimal, Decimal]:
    db.expire_all()
    set_tenant(tenant_id)
    try:
        o = db.query(OdOrder).filter_by(id=order_id, tenant_id=tenant_id).one()
        return o.goods_amount, o.pay_amount
    finally:
        reset()


def test_create_order_ignores_client_amount_fields(client, db_session):
    base = _base()
    member_a = base + 90
    _seed_member(db_session, TENANT_A, member_a)
    item_a = _seed_goods(db_session, TENANT_A, base, total=20, price="12.50")
    item_b = _seed_goods(db_session, TENANT_A, base + 1000, total=20, price="9.90")
    ca = _customer(member_a, TENANT_A)

    body = assert_biz_code(client.post("/api/c/orders", headers=ca, json={
        "sku_id": item_a["skuId"], "quantity": 2, "receiver_name": "张三", "receiver_phone": "13800000000",
        "payAmount": "0.01", "amount": "0.01", "price": "0.01", "unitPrice": "0.01",
        "goodsAmount": "0.03", "discountAmount": "99.99", "freightAmount": "-5",
    }), BizCode.OK)["data"]
    order_id = body["id"]
    assert body["payAmount"] == "25.00"
    detail = assert_biz_code(client.get(f"/api/c/order/{order_id}", headers=ca), BizCode.OK)["data"]
    assert detail["payAmount"] == "25.00"
    assert detail["items"][0]["price"] == "12.50" and detail["items"][0]["subtotalAmount"] == "25.00"
    assert _order_amount(db_session, TENANT_A, order_id) == (Decimal("25.00"), Decimal("25.00"))

    preview = assert_biz_code(client.post("/api/c/order/preview", headers=ca, json={
        "items": [{"skuId": item_b["skuId"], "quantity": 3, "price": "0.01", "subtotalAmount": "0.03"}],
        "payAmount": "0.01",
    }), BizCode.OK)["data"]
    assert preview["payAmount"] == "29.70" and preview["goodsAmount"] == "29.70"

    compat = assert_biz_code(client.post("/api/c/order/create", headers=ca, json={
        "skuId": item_b["skuId"], "quantity": 1, "name": "李四", "phone": "13900000000",
        "payAmount": "0.01", "amount": "0.02", "price": "0.01",
    }), BizCode.OK)["data"]
    assert compat["payAmount"] == "9.90"


def test_off_sale_goods_rejected(client, db_session):
    base = _base()
    member_a = base + 91
    _seed_member(db_session, TENANT_A, member_a)
    off_sale = _seed_goods(db_session, TENANT_A, base, status="OFF_SALE", total=5)
    not_listed = _seed_goods(db_session, TENANT_A, base + 1000, status="ON_SALE", normal_on_sale=0, total=5)
    ca = _customer(member_a, TENANT_A)

    for item in (off_sale, not_listed):
        assert_biz_code(client.post("/api/c/orders", headers=ca, json={
            "sku_id": item["skuId"], "quantity": 1,
        }), BizCode.ORDER_GOODS_OFF_SALE)
        assert_biz_code(client.post("/api/c/order/preview", headers=ca, json={
            "items": [{"skuId": item["skuId"], "quantity": 1}],
        }), BizCode.ORDER_GOODS_OFF_SALE)

    assert _order_count(db_session, TENANT_A, member_a) == 0
    for item in (off_sale, not_listed):
        assert _stock(db_session, TENANT_A, item["skuId"]) == (5, 0, 0, 5)


def test_over_stock_rejected_without_locking(client, db_session):
    base = _base()
    member_a = base + 92
    _seed_member(db_session, TENANT_A, member_a)
    item = _seed_goods(db_session, TENANT_A, base, total=5)
    ca = _customer(member_a, TENANT_A)

    assert_biz_code(client.post("/api/c/order/preview", headers=ca, json={
        "items": [{"skuId": item["skuId"], "quantity": 6}],
    }), BizCode.STOCK_NOT_ENOUGH)
    assert_biz_code(client.post("/api/c/orders", headers=ca, json={
        "sku_id": item["skuId"], "quantity": 6,
    }), BizCode.STOCK_NOT_ENOUGH)
    assert _stock(db_session, TENANT_A, item["skuId"]) == (5, 0, 0, 5)
    assert _order_count(db_session, TENANT_A, member_a) == 0

    ok_body = assert_biz_code(client.post("/api/c/orders", headers=ca, json={
        "sku_id": item["skuId"], "quantity": 5,
    }), BizCode.OK)["data"]
    assert ok_body["payAmount"] == "62.50"
    assert _stock(db_session, TENANT_A, item["skuId"]) == (5, 5, 0, 0)

    assert_biz_code(client.post("/api/c/orders", headers=ca, json={
        "sku_id": item["skuId"], "quantity": 1,
    }), BizCode.STOCK_NOT_ENOUGH)
    assert _stock(db_session, TENANT_A, item["skuId"]) == (5, 5, 0, 0)
    set_tenant(TENANT_A)
    try:
        assert db_session.query(OdOrderItem).filter_by(
            tenant_id=TENANT_A, sku_id=item["skuId"]).count() == 1
    finally:
        reset()


def test_cross_tenant_purchase_rejected(client, db_session):
    base = _base()
    member_a, member_b = base + 93, base + 94
    _seed_member(db_session, TENANT_A, member_a)
    _seed_member(db_session, TENANT_B, member_b)
    item_b = _seed_goods(db_session, TENANT_B, base, total=8)
    ca = _customer(member_a, TENANT_A)

    resp = client.post("/api/c/orders", headers=ca, json={
        "sku_id": item_b["skuId"], "quantity": 2,
    })
    assert resp.status_code in (200, 404)
    body = resp.json()
    if resp.status_code != 404:
        assert body["code"] == BizCode.ORDER_SKU_INVALID, f"期望明确拒绝，实际 {body}"

    assert_biz_code(client.post("/api/c/order/preview", headers=ca, json={
        "items": [{"skuId": item_b["skuId"], "quantity": 1}],
    }), BizCode.ORDER_SKU_INVALID)

    assert _stock(db_session, TENANT_B, item_b["skuId"]) == (8, 0, 0, 8)
    assert _order_count(db_session, TENANT_A, member_a) == 0
    assert _order_count(db_session, TENANT_B, member_b) == 0


def test_idempotency_key_deduplication(client, db_session):
    base = _base()
    member_a = base + 95
    _seed_member(db_session, TENANT_A, member_a)
    item = _seed_goods(db_session, TENANT_A, base, total=10)
    ca = _customer(member_a, TENANT_A)
    idem = f"qa-idem-{uuid.uuid4().hex[:24]}"
    payload = {"sku_id": item["skuId"], "quantity": 2}

    first = assert_biz_code(client.post("/api/c/orders", headers={**ca, "Idempotency-Key": idem}, json=payload), BizCode.OK)["data"]
    second = assert_biz_code(client.post("/api/c/orders", headers={**ca, "Idempotency-Key": idem}, json=payload), BizCode.OK)["data"]
    assert second["id"] == first["id"] and second["orderNo"] == first["orderNo"]
    assert second["payAmount"] == "25.00"

    assert _stock(db_session, TENANT_A, item["skuId"]) == (10, 2, 0, 8)
    db_session.expire_all()
    set_tenant(TENANT_A)
    try:
        rows = db_session.query(OdOrder).filter_by(tenant_id=TENANT_A, member_id=member_a).all()
        assert len(rows) == 1 and rows[0].idempotency_key == idem and rows[0].status == "PENDING_PAY"
    finally:
        reset()
    listing = assert_biz_code(client.get("/api/c/orders", headers=ca), BizCode.OK)["data"]
    assert len(listing) == 1


def test_stock_lock_and_release_on_cancel(client, db_session):
    base = _base()
    member_a = base + 96
    _seed_member(db_session, TENANT_A, member_a)
    item = _seed_goods(db_session, TENANT_A, base, total=10)
    ca = _customer(member_a, TENANT_A)

    created = assert_biz_code(client.post("/api/c/orders", headers=ca, json={
        "sku_id": item["skuId"], "quantity": 3,
    }), BizCode.OK)["data"]
    order_id = created["id"]
    assert _stock(db_session, TENANT_A, item["skuId"]) == (10, 3, 0, 7)

    closed = assert_biz_code(client.post(f"/api/c/order/{order_id}/cancel", headers=ca), BizCode.OK)["data"]
    assert closed["status"] == "CLOSED"
    assert _stock(db_session, TENANT_A, item["skuId"]) == (10, 0, 0, 10)

    again = assert_biz_code(client.post(f"/api/c/order/{order_id}/cancel", headers=ca), BizCode.OK)["data"]
    assert again["status"] == "CLOSED"
    assert _stock(db_session, TENANT_A, item["skuId"]) == (10, 0, 0, 10)

    rebuy = assert_biz_code(client.post("/api/c/orders", headers=ca, json={
        "sku_id": item["skuId"], "quantity": 3,
    }), BizCode.OK)["data"]
    assert rebuy["id"] != order_id and rebuy["payAmount"] == "37.50"
