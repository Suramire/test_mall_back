"""用户端 P1：积分商城（POINTS 通道）与商品搜索验收。

覆盖 03-API设计.md §5.2/§5.4：
- GET /api/c/points-goods           列表（pure/mixed，含 limit/stock）
- GET /api/c/points-goods/{id}      兑换详情（按钮三态）
- POST /api/c/points-order          ⚡积分下单（PT 单号、库存预锁、积分余额校验）
- GET /api/c/search                 商品搜索（NORMAL 在售）
"""
from __future__ import annotations

from decimal import Decimal

from app.core.errors import BizCode
from app.core.security import SCOPE_CUSTOMER, create_access_token
from app.core.tenant_context import reset, set_tenant
from app.models.gd_goods import GdGoods, GdSku, GdSkuStock
from app.models.mb_member import MbMember
from tests.conftest import assert_biz_code

# 每用例独立租户 id，避免跨用例冲突
_CASE_TID = 6001


def _enable_sqlite_autoincrement() -> None:
    from sqlalchemy import BigInteger, Integer

    from app.db.base import Base
    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if isinstance(col.type, BigInteger):
                col.type = BigInteger().with_variant(Integer(), "sqlite")


_enable_sqlite_autoincrement()


def _headers(member_id: int, tenant_id: int) -> dict[str, str]:
    return {"Authorization": "Bearer " + create_access_token(str(member_id), SCOPE_CUSTOMER, tenant_id=tenant_id)}


def _seed(db_session, tid: int, member_id: int, points: int = 1000):
    """造租户：积分商品(纯积分)+混合商品(积分+补差)+普通商品(仅 NORMAL)。"""
    set_tenant(tid)
    try:
        db_session.add(MbMember(id=member_id, tenant_id=tid, member_no=f"QA{tid}-M", nickname="积分买家", points_balance=points))
        g1 = GdGoods(id=tid * 10 + 1, tenant_id=tid, name="积分水杯", subtitle="纯积分", type="PHYSICAL",
                     channel="POINTS", status="ON_SALE", points_on_sale=1, main_image="img1")
        g2 = GdGoods(id=tid * 10 + 2, tenant_id=tid, name="积分耳机", subtitle="混合", type="PHYSICAL",
                     channel="BOTH", status="ON_SALE", points_on_sale=1, normal_on_sale=1, main_image="img2")
        g3 = GdGoods(id=tid * 10 + 3, tenant_id=tid, name="普通苹果", subtitle="NORMAL", type="PHYSICAL",
                     channel="NORMAL", status="ON_SALE", normal_on_sale=1, main_image="img3")
        db_session.add_all([g1, g2, g3])
        db_session.flush()
        db_session.add_all([
            GdSku(id=tid * 100 + 1, tenant_id=tid, goods_id=g1.id, sku_code=f"SKU{tid}1", price=Decimal("0.00"), points=500, cash=Decimal("0.00")),
            GdSku(id=tid * 100 + 2, tenant_id=tid, goods_id=g2.id, sku_code=f"SKU{tid}2", price=Decimal("9.90"), points=300, cash=Decimal("9.90")),
            GdSku(id=tid * 100 + 3, tenant_id=tid, goods_id=g3.id, sku_code=f"SKU{tid}3", price=Decimal("5.00"), points=0, cash=Decimal("0.00")),
        ])
        db_session.flush()
        db_session.add_all([
            GdSkuStock(tenant_id=tid, goods_id=g1.id, sku_id=tid * 100 + 1, channel="POINTS", total_stock=10, available_stock=10, locked_stock=0, sold_stock=0),
            GdSkuStock(tenant_id=tid, goods_id=g2.id, sku_id=tid * 100 + 2, channel="POINTS", total_stock=10, available_stock=10, locked_stock=0, sold_stock=0),
            GdSkuStock(tenant_id=tid, goods_id=g2.id, sku_id=tid * 100 + 2, channel="NORMAL", total_stock=10, available_stock=10, locked_stock=0, sold_stock=0),
            GdSkuStock(tenant_id=tid, goods_id=g3.id, sku_id=tid * 100 + 3, channel="NORMAL", total_stock=5, available_stock=5, locked_stock=0, sold_stock=0),
        ])
        db_session.commit()
    finally:
        reset()


def test_points_goods_list_and_search(client, db_session):
    tid = 6001
    member = 600_001
    _seed(db_session, tid, member)
    h = _headers(member, tid)
    # 积分商城列表：仅 POINTS 渠道在售（g1、g2），含 stock/limit
    data = assert_biz_code(client.get("/api/c/points-goods", headers=h), BizCode.OK)["data"]
    assert data["total"] == 2
    names = {x["name"] for x in data["list"]}
    assert names == {"积分水杯", "积分耳机"}
    g2 = next(x for x in data["list"] if x["name"] == "积分耳机")
    assert g2["stock"] == 10 and g2["points"] == 300 and g2["cash"] == "9.90"
    assert g2["priceMode"] == "MIXED"
    g1 = next(x for x in data["list"] if x["name"] == "积分水杯")
    assert g1["priceMode"] == "POINTS" and g1["points"] == 500
    # 搜索：NORMAL 在售
    sdata = assert_biz_code(client.get("/api/c/search", params={"keyword": "苹果"}, headers=h), BizCode.OK)["data"]
    assert sdata["total"] == 1 and sdata["list"][0]["name"] == "普通苹果"


def test_points_goods_detail_button_states(client, db_session):
    tid = 6002
    member = 600_002
    _seed(db_session, tid, member, points=100)
    h = _headers(member, tid)
    # 余额 100：积分水杯 500 → 余额不足
    d1 = assert_biz_code(client.get(f"/api/c/points-goods/{tid*10+1}", headers=h), BizCode.OK)["data"]
    assert d1["buttonState"] == "INSUFFICIENT"
    # 混合耳机 300 积分 → 依然余额不足
    d2 = assert_biz_code(client.get(f"/api/c/points-goods/{tid*10+2}", headers=h), BizCode.OK)["data"]
    assert d2["buttonState"] == "INSUFFICIENT"
    # 下架商品 → NOT_FOUND
    set_tenant(tid)
    try:
        g3 = db_session.get(GdGoods, tid * 10 + 3)
        g3.points_on_sale = 0
        db_session.commit()
    finally:
        reset()
    assert_biz_code(client.get(f"/api/c/points-goods/{tid*10+3}", headers=h), BizCode.NOT_FOUND)


def test_points_order_create_and_idempotency(client, db_session):
    tid = 6003
    member = 600_003
    _seed(db_session, tid, member, points=1000)
    h = _headers(member, tid)
    # 混合商品下单：300 积分 + 9.90 现金 × 2 = 600 积分 + 19.80（首次带幂等键）
    r = assert_biz_code(client.post("/api/c/points-order", headers={**h, "Idempotency-Key": "k"}, json={
        "skuId": tid * 100 + 2, "quantity": 2,
        "deliveryType": "EXPRESS", "receiverName": "张三", "receiverPhone": "13800000000",
    }), BizCode.OK)["data"]
    assert r["orderNo"].startswith("PT") and r["payPoints"] == 600 and r["payAmount"] == "19.80"
    # 幂等：同 Idempotency-Key 返回既有订单（不重复锁库）
    r2 = assert_biz_code(client.post("/api/c/points-order", headers={**h, "Idempotency-Key": "k"}, json={
        "skuId": tid * 100 + 2, "quantity": 2,
    }), BizCode.OK)["data"]
    assert r2["id"] == r["id"]
    # 库存已预锁（10-2=8，幂等单不重复扣）
    set_tenant(tid)
    try:
        st = db_session.query(GdSkuStock).filter_by(tenant_id=tid, sku_id=tid * 100 + 2, channel="POINTS").first()
        assert st.available_stock == 8 and st.locked_stock == 2
    finally:
        reset()


def test_points_order_insufficient_balance_and_stock(client, db_session):
    tid = 6004
    member = 600_004
    _seed(db_session, tid, member, points=100)
    h = _headers(member, tid)
    # 积分不足 → 45001
    r = client.post("/api/c/points-order", headers=h, json={"skuId": tid * 100 + 1, "quantity": 1})
    assert r.status_code == 200 and r.json()["code"] == BizCode.POINTS_NOT_ENOUGH
    # 非 POINTS 渠道商品（NORMAL）→ 43008 商品已失效
    r2 = client.post("/api/c/points-order", headers=h, json={"skuId": tid * 100 + 3, "quantity": 1})
    assert r2.status_code == 200 and r2.json()["code"] == BizCode.ORDER_GOODS_OFF_SALE


def test_points_order_zero_stock(client, db_session):
    """POINTS 渠道商品但库存为 0 → 42008 库存不足。"""
    tid = 6005
    member = 600_005
    _seed(db_session, tid, member, points=1000)
    set_tenant(tid)
    try:
        st = db_session.query(GdSkuStock).filter_by(tenant_id=tid, sku_id=tid * 100 + 1, channel="POINTS").first()
        st.available_stock = 0
        db_session.commit()
    finally:
        reset()
    h = _headers(member, tid)
    r = client.post("/api/c/points-order", headers=h, json={"skuId": tid * 100 + 1, "quantity": 1})
    assert r.status_code == 200 and r.json()["code"] == BizCode.STOCK_NOT_ENOUGH
