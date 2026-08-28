"""缺陷回归：B（STOCKED 备货中禁止直接发货）+ D（TICKET/VIRTUAL 支付后生成核销码）。

- 缺陷 B：ship_order/batch_ship 此前允许 STOCKED 订单直接发货（备货中误发物流）。
- 缺陷 D：TICKET 商品支付成功后不生成 OdVerifyCode，核销链路断裂；
  设计文档规定「购买 N 张生成 N 条」，用户端/商家端订单详情应返回核销码。
"""
from __future__ import annotations

import uuid

from app.core.security import SCOPE_CUSTOMER, SCOPE_MERCHANT, create_access_token
from app.core.tenant_context import reset, set_tenant
from app.models.gd_goods import GdGoods, GdSku, GdSkuStock
from app.models.mb_member import MbMember
from app.models.od_order import OdOrder, OdOrderItem, OdVerifyCode
from tests.conftest import assert_biz_code


def _enable_sqlite_autoincrement() -> None:
    from sqlalchemy import BigInteger, Integer

    from app.db.base import Base
    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if isinstance(col.type, BigInteger):
                col.type = BigInteger().with_variant(Integer(), "sqlite")


_enable_sqlite_autoincrement()


def _customer(mid: int, tid: int) -> dict[str, str]:
    return {"Authorization": "Bearer " + create_access_token(str(mid), SCOPE_CUSTOMER, tenant_id=tid)}


def _merchant(tid: int) -> dict[str, str]:
    return {"Authorization": "Bearer " + create_access_token("10", SCOPE_MERCHANT, tenant_id=tid, perms=["MC_ALL"])}


def _seed_ticket(db, base: int, *, price: str = "10.00", gtype: str = "TICKET") -> tuple[int, int]:
    """租户 1001 落 TICKET/VIRTUAL 商品/SKU/库存/会员，返回 (sku_id, member_id)。"""
    set_tenant(1001)
    try:
        gid, sid, mid = base + 1, base + 2, base + 3
        kw = {"virtual_desc": "卡密"} if gtype == "VIRTUAL" else {"verify_desc": "到店出示核销码"}
        db.add(GdGoods(id=gid, tenant_id=1001, name=f"{gtype}测试商品", type=gtype,
                       channel="NORMAL", status="ON_SALE", normal_on_sale=1,
                       valid_type="DAYS_AFTER_PAY", valid_days=30, **kw))
        db.add(GdSku(id=sid, tenant_id=1001, goods_id=gid, sku_code=f"TK-{sid}", price=price))
        db.add(GdSkuStock(tenant_id=1001, goods_id=gid, sku_id=sid, channel="NORMAL",
                          total_stock=100, available_stock=100))
        db.add(MbMember(id=mid, tenant_id=1001, member_no=f"QAT{mid}", points_balance=100))
        db.commit()
        return sid, mid
    finally:
        reset()


def _buy(db, client, sku_id: int, mid: int, quantity: int = 1) -> dict:
    h = _customer(mid, 1001)
    from app.core.errors import BizCode
    created = assert_biz_code(client.post("/api/c/order/create", headers=h, json={
        "skuId": sku_id, "quantity": quantity, "deliveryType": "EXPRESS",
        "receiverName": "QA", "receiverPhone": "13800000000", "receiverAddress": "QA地址"}), BizCode.OK)["data"]
    oid = created["id"]
    paid = assert_biz_code(client.post(f"/api/c/order/{oid}/pay", headers=h, json={}), BizCode.OK)["data"]
    return {"oid": oid, "paid": paid, "header": h}


def test_ticket_order_pay_generates_verify_codes(client, db_session):
    """TICKET 商品支付成功后按购买数量生成核销码（HX 前缀），详情返回，重复支付幂等。"""
    base = 430_000_000 + int(uuid.uuid4().int % 10_000_000)
    sku_id, mid = _seed_ticket(db_session, base)
    res = _buy(db_session, client, sku_id, mid, quantity=2)
    oid = res["oid"]

    set_tenant(1001)
    try:
        codes = db_session.query(OdVerifyCode).filter_by(tenant_id=1001, order_id=oid).all()
        assert len(codes) == 2, f"期望 2 条核销码，实际 {len(codes)}"
        for c in codes:
            assert c.code.startswith("HX")
            assert c.code_type == "VERIFY"
            assert c.status == "UNUSED"
            assert c.valid_end > c.valid_start
        before = len(codes)
    finally:
        reset()

    # 幂等：重复支付不重复生成
    client.post(f"/api/c/order/{oid}/pay", headers=res["header"], json={})
    set_tenant(1001)
    try:
        after = db_session.query(OdVerifyCode).filter_by(tenant_id=1001, order_id=oid).count()
        assert after == before, "重复支付不应重复生成核销码"
    finally:
        reset()

    # 用户端详情返回 verifyCodes
    r = client.get(f"/api/c/order/{oid}", headers=res["header"])
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data.get("verifyCodes", [])) == 2
    assert data["verifyCodes"][0]["code"].startswith("HX")


def test_stocked_order_cannot_ship(client, db_session):
    """备货中（STOCKED）订单不允许直接发货，必须走备货完成流转。"""
    base = 440_000_000 + int(uuid.uuid4().int % 10_000_000)
    sid, mid = _seed_ticket(db_session, base, gtype="NORMAL")
    h = _merchant(1001)

    set_tenant(1001)
    try:
        o = OdOrder(tenant_id=1001, order_no=f"ORD-STK-{base}", channel="NORMAL",
                    member_id=mid, status="STOCKED", delivery_type="EXPRESS",
                    goods_amount="10.00", pay_amount="10.00",
                    receiver_name="QA", receiver_phone="13800000000", receiver_address="QA地址")
        db_session.add(o)
        db_session.commit()
        oid = o.id
    finally:
        reset()

    r = client.post(f"/api/mc/order/{oid}/ship", json={"expressCompany": "SF", "expressNo": "SF123"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["code"] == 40001, r.text
    assert "不可发货" in r.json()["message"]

    r = client.post("/api/mc/order/batch-ship", json={"ids": [oid], "expressCompany": "SF", "expressNo": "SF123"}, headers=h)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["success"] == []
    assert data["failed"][0]["message"] == "订单当前状态不可发货"

    # 备货完成后（PENDING_SHIP）可发货
    set_tenant(1001)
    try:
        db_session.query(OdOrder).filter_by(id=oid).update({"status": "PENDING_SHIP"})
        db_session.commit()
    finally:
        reset()
    r = client.post(f"/api/mc/order/{oid}/ship", json={"expressCompany": "SF", "expressNo": "SF123"}, headers=h)
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "SHIPPED"

    set_tenant(1001)
    try:
        db_session.query(OdOrder).filter_by(id=oid).delete()
        db_session.query(GdSkuStock).filter_by(sku_id=sid).delete()
        db_session.query(GdSku).filter_by(id=sid).delete()
        db_session.query(GdGoods).filter_by(id=base + 1).delete()
        db_session.commit()
    finally:
        reset()


def test_virtual_order_pay_generates_voucher_codes(client, db_session):
    """VIRTUAL 商品支付后生成 VC 券码。"""
    base = 450_000_000 + int(uuid.uuid4().int % 10_000_000)
    sku_id, mid = _seed_ticket(db_session, base, gtype="VIRTUAL")
    res = _buy(db_session, client, sku_id, mid, quantity=1)
    oid = res["oid"]

    set_tenant(1001)
    try:
        codes = db_session.query(OdVerifyCode).filter_by(tenant_id=1001, order_id=oid).all()
        assert len(codes) == 1
        assert codes[0].code.startswith("VC")
        assert codes[0].code_type == "VIRTUAL"
    finally:
        reset()
