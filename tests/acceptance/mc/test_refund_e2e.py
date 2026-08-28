"""商家退款审批端到端回归：真实下单-支付-申请-审批全链路。

覆盖：审批通过（状态流转+库存三段回补+积分回滚双端流水读回）、
审批驳回（回到可履约状态+库存与积分不动）、重复审批拒绝、
非法状态申请退款、跨租户越权负例。
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from app.core.errors import BizCode
from app.core.security import SCOPE_CUSTOMER, SCOPE_MERCHANT, create_access_token
from app.core.tenant_context import reset, set_tenant
from app.models.gd_goods import GdGoods, GdSku, GdSkuStock, GdStockLog
from app.models.mb_member import MbMember, MbPointsLog
from app.models.od_order import OdOrder, OdRefund
from tests.conftest import assert_biz_code


def _enable_sqlite_autoincrement() -> None:
    from sqlalchemy import BigInteger, Integer

    from app.db.base import Base
    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if isinstance(col.type, BigInteger):
                col.type = BigInteger().with_variant(Integer(), "sqlite")


_enable_sqlite_autoincrement()


def _merchant(tid: int) -> dict[str, str]:
    return {"Authorization": "Bearer " + create_access_token("10", SCOPE_MERCHANT, tenant_id=tid, perms=["MC_ALL"])}


def _customer(mid: int, tid: int) -> dict[str, str]:
    return {"Authorization": "Bearer " + create_access_token(str(mid), SCOPE_CUSTOMER, tenant_id=tid)}


def _stock(db, sku_id: int):
    return db.query(GdSkuStock).filter_by(sku_id=sku_id, channel="NORMAL").one()


def _seed(db, base: int, *, total: int = 10, balance: int = 5) -> int:
    """租户 1001 落商品/SKU/库存/会员，返回 sku_id。"""
    set_tenant(1001)
    try:
        gid, sid, mid = base + 1, base + 2, base + 3
        db.add(GdGoods(id=gid, tenant_id=1001, name="E2E退款商品", type="NORMAL",
                       channel="NORMAL", status="ON_SALE", normal_on_sale=1))
        db.add(GdSku(id=sid, tenant_id=1001, goods_id=gid, sku_code=f"E2E-{sid}", price="12.50"))
        db.add(GdSkuStock(tenant_id=1001, goods_id=gid, sku_id=sid, channel="NORMAL",
                          total_stock=total, available_stock=total))
        db.add(MbMember(id=mid, tenant_id=1001, member_no=f"E2EM{mid}", points_balance=balance))
        db.commit()
        return sid
    finally:
        reset()


def _buy(db, client, sku_id: int, mid: int) -> dict:
    """真实 API 下单并支付，返回订单接口响应 data。"""
    h = _customer(mid, 1001)
    created = assert_biz_code(client.post("/api/c/order/create", headers=h, json={
        "skuId": sku_id, "quantity": 1, "deliveryType": "EXPRESS",
        "receiverName": "QA", "receiverPhone": "13800000000", "receiverAddress": "QA地址"}), BizCode.OK)["data"]
    oid = created["id"]
    assert created["status"] == "PENDING_PAY"
    paid = assert_biz_code(client.post(f"/api/c/order/{oid}/pay", headers=h, json={}), BizCode.OK)["data"]
    assert paid["status"] == "PAID"
    return created


def _apply_refund(client, oid: int, mid: int) -> dict:
    body = assert_biz_code(client.post("/api/c/refund", headers=_customer(mid, 1001),
                                       json={"orderId": oid, "reasonCode": "QA", "reasonDesc": "E2E退款"}), BizCode.OK)["data"]
    return body


def test_e2e_approve_stock_return_and_points_readback(client, db_session):
    base = 310_000_000 + int(uuid.uuid4().int % 10_000_000)
    sku_id = _seed(db_session, base, total=10, balance=5)
    mid = base + 3
    created = _buy(db_session, client, sku_id, mid)
    oid = created["id"]

    set_tenant(1001)
    try:
        row = _stock(db_session, sku_id)
        assert (row.total_stock, row.locked_stock, row.sold_stock, row.available_stock) == (10, 0, 1, 9)
        # 支付成功后自动发放积分：pay_amount 12.50 * POINTS_EARN_RATE(1) = 12 分（下单即发放，无需手工种子）
        m0 = db_session.query(MbMember).filter_by(id=mid, tenant_id=1001).one()
        assert m0.points_balance == 5 + 12
    finally:
        reset()

    refund = _apply_refund(client, oid, mid)
    rid = refund["id"]
    set_tenant(1001)
    try:
        assert db_session.query(OdOrder).filter_by(id=oid, tenant_id=1001).one().status == "REFUNDING"
        assert _stock(db_session, sku_id).available_stock == 9
    finally:
        reset()
    assert_biz_code(client.post("/api/c/refund", headers=_customer(mid, 1001),
                                json={"orderId": oid}), BizCode.REFUND_DUPLICATE)

    ha = _merchant(1001)
    preview = assert_biz_code(client.get(f"/api/mc/refund/{rid}/rollback-preview", headers=ha), BizCode.OK)["data"]
    assert preview["orderEarnedPoints"] == 12 and preview["currentBalance"] == 17 and preview["debt"] == 0
    detail = assert_biz_code(client.get(f"/api/mc/refund/{rid}", headers=ha), BizCode.OK)["data"]
    assert detail["status"] == "PENDING_AUDIT" and detail["refundAmount"] == "12.50"
    listed = assert_biz_code(client.get("/api/mc/refund?status=PENDING_AUDIT", headers=ha), BizCode.OK)["data"]
    assert any(x["id"] == rid for x in listed)

    assert_biz_code(client.post(f"/api/mc/refund/{rid}/approve", headers=ha), BizCode.OK)

    set_tenant(1001)
    try:
        db_session.expire_all()
        r = db_session.query(OdRefund).filter_by(id=rid, tenant_id=1001).one()
        o = db_session.query(OdOrder).filter_by(id=oid, tenant_id=1001).one()
        m = db_session.query(MbMember).filter_by(id=mid, tenant_id=1001).one()
        row = _stock(db_session, sku_id)
        assert r.status == "APPROVED" and r.rollback_points == 12 and r.rollback_debt == 0
        assert o.status == "REFUNDED"
        assert (row.total_stock, row.locked_stock, row.sold_stock, row.available_stock) == (10, 0, 0, 10)
        assert row.available_stock == row.total_stock - row.locked_stock - row.sold_stock
        returns = db_session.query(GdStockLog).filter_by(tenant_id=1001, sku_id=sku_id,
                                                         change_type="REFUND_RETURN").all()
        assert len(returns) == 1 and returns[0].change_val == 1 and returns[0].after_val == 10
        assert m.points_balance == 5 and m.points_debt == 0
    finally:
        reset()

    clog = assert_biz_code(client.get("/api/c/points/log", headers=_customer(mid, 1001)), BizCode.OK)["data"]
    rollback_rows = [x for x in clog["list"] if x["changeType"] == "REFUND_ROLLBACK"]
    assert len(rollback_rows) == 1 and rollback_rows[0]["amount"] == -12 and rollback_rows[0]["balanceAfter"] == 5
    mlog = assert_biz_code(client.get(f"/api/mc/points/log?memberId={mid}", headers=ha), BizCode.OK)["data"]
    m_rollback = [x for x in mlog["list"] if x["changeType"] == "REFUND_ROLLBACK"]
    assert len(m_rollback) == 1 and m_rollback[0]["amount"] == -12

    assert_biz_code(client.post(f"/api/mc/refund/{rid}/approve", headers=ha), BizCode.CONFLICT)
    assert_biz_code(client.post(f"/api/mc/refund/{rid}/reject", headers=ha, json={"rejectReason": "x"}), BizCode.CONFLICT)


def test_e2e_reject_keeps_stock_and_resumes_fulfilment(client, db_session):
    base = 320_000_000 + int(uuid.uuid4().int % 10_000_000)
    sku_id = _seed(db_session, base, total=10, balance=7)
    mid = base + 3
    oid = _buy(db_session, client, sku_id, mid)["id"]
    rid = _apply_refund(client, oid, mid)["id"]
    ha = _merchant(1001)

    assert_biz_code(client.post(f"/api/mc/refund/{rid}/reject", headers=ha, json={"rejectReason": "凭证不足"}), BizCode.OK)

    set_tenant(1001)
    try:
        r = db_session.query(OdRefund).filter_by(id=rid, tenant_id=1001).one()
        o = db_session.query(OdOrder).filter_by(id=oid, tenant_id=1001).one()
        m = db_session.query(MbMember).filter_by(id=mid, tenant_id=1001).one()
        row = _stock(db_session, sku_id)
        assert r.status == "REJECTED" and r.reject_reason == "凭证不足"
        assert o.status == "PAID"
        assert (row.total_stock, row.locked_stock, row.sold_stock, row.available_stock) == (10, 0, 1, 9)
        # 支付时自动发放 12 分（12.50*1），驳回不动积分：余额=7+12=19
        assert m.points_balance == 19 and m.points_debt == 0
        assert db_session.query(GdStockLog).filter_by(tenant_id=1001, sku_id=sku_id,
                                                      change_type="REFUND_RETURN").count() == 0
        assert db_session.query(MbPointsLog).filter_by(tenant_id=1001, member_id=mid,
                                                       change_type="REFUND_ROLLBACK").count() == 0
    finally:
        reset()
    shipped = assert_biz_code(client.post(f"/api/mc/order/{oid}/ship", headers=ha,
                                          json={"expressCompany": "SF", "expressNo": "SF123"}), BizCode.OK)["data"]
    assert shipped["status"] == "SHIPPED"


def test_e2e_reapply_after_reject_returns_stock_once(client, db_session):
    base = 330_000_000 + int(uuid.uuid4().int % 10_000_000)
    sku_id = _seed(db_session, base, total=6, balance=0)
    mid = base + 3
    oid = _buy(db_session, client, sku_id, mid)["id"]
    ha = _merchant(1001)
    rid1 = _apply_refund(client, oid, mid)["id"]
    assert_biz_code(client.post(f"/api/mc/refund/{rid1}/reject", headers=ha, json={}), BizCode.OK)

    rid2 = _apply_refund(client, oid, mid)["id"]
    assert rid2 != rid1
    assert_biz_code(client.post(f"/api/mc/refund/{rid2}/approve", headers=ha), BizCode.OK)

    set_tenant(1001)
    try:
        row = _stock(db_session, sku_id)
        assert (row.total_stock, row.locked_stock, row.sold_stock, row.available_stock) == (6, 0, 0, 6)
        assert db_session.query(GdStockLog).filter_by(tenant_id=1001, sku_id=sku_id,
                                                      change_type="REFUND_RETURN").count() == 1
        assert db_session.query(OdOrder).filter_by(id=oid, tenant_id=1001).one().status == "REFUNDED"
    finally:
        reset()


def test_e2e_invalid_state_requests_rejected(client, db_session):
    base = 340_000_000 + int(uuid.uuid4().int % 10_000_000)
    sku_id = _seed(db_session, base, total=4, balance=0)
    mid = base + 3
    h = _customer(mid, 1001)
    set_tenant(1001)
    try:
        closed_oid, completed_oid = base + 10, base + 11
        for oid, st in ((closed_oid, "CLOSED"), (completed_oid, "COMPLETED")):
            db_session.add(OdOrder(id=oid, tenant_id=1001, order_no=f"E2E-X-{oid}", channel="NORMAL",
                                   member_id=mid, status=st, delivery_type="EXPRESS",
                                   goods_amount=Decimal("12.50"), pay_amount=Decimal("12.50")))
        db_session.commit()
    finally:
        reset()

    assert_biz_code(client.post("/api/c/refund", headers=h, json={"orderId": closed_oid}), BizCode.ORDER_STATUS_INVALID)
    assert_biz_code(client.post("/api/c/refund", headers=h, json={"orderId": completed_oid}), BizCode.ORDER_STATUS_INVALID)

    oid = _buy(db_session, client, sku_id, mid)["id"]
    pending_pay = assert_biz_code(client.post("/api/c/order/create", headers=h, json={
        "skuId": sku_id, "quantity": 1, "receiverName": "QA", "receiverPhone": "138"}), BizCode.OK)["data"]["id"]
    assert_biz_code(client.post("/api/c/refund", headers=h, json={"orderId": pending_pay}), BizCode.ORDER_STATUS_INVALID)


def test_e2e_cross_tenant_merchant_blocked(client, db_session):
    base = 350_000_000 + int(uuid.uuid4().int % 10_000_000)
    sku_id = _seed(db_session, base, total=8, balance=0)
    mid = base + 3
    oid = _buy(db_session, client, sku_id, mid)["id"]
    rid = _apply_refund(client, oid, mid)["id"]
    hb = _merchant(2002)

    assert_biz_code(client.post(f"/api/mc/refund/{rid}/approve", headers=hb), BizCode.NOT_FOUND)
    assert_biz_code(client.post(f"/api/mc/refund/{rid}/reject", headers=hb, json={}), BizCode.NOT_FOUND)
    assert_biz_code(client.get(f"/api/mc/refund/{rid}", headers=hb), BizCode.NOT_FOUND)
    assert_biz_code(client.get(f"/api/mc/refund/{rid}/rollback-preview", headers=hb), BizCode.NOT_FOUND)
    other_list = assert_biz_code(client.get("/api/mc/refund", headers=hb), BizCode.OK)["data"]
    assert all(x["id"] != rid for x in other_list)

    set_tenant(1001)
    try:
        assert db_session.query(OdRefund).filter_by(id=rid, tenant_id=1001).one().status == "PENDING_AUDIT"
        assert _stock(db_session, sku_id).sold_stock == 1
    finally:
        reset()
