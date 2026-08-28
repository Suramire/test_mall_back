"""下单积分发放与支付积分抵扣验收（真实 API + 数据持久化，无 mock）。

覆盖：
① 支付时传入 pointsDeduct：会员积分减少、应付金额按 POINTS_REDUCE_RATIO 降低；
② 积分不足时支付被拒（明确业务错误，不静默忽略）；
③ 订单支付完成后会员按 POINTS_EARN_RATE 增加积分，且重复触发（确认收货）不重复发放；
④ 退款通过后已发放积分被回退（REFUND_ROLLBACK）。
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from app.core.errors import BizCode
from app.core.security import SCOPE_CUSTOMER, SCOPE_MERCHANT, create_access_token
from app.core.tenant_context import reset, set_tenant
from app.models.gd_goods import GdGoods, GdSku, GdSkuStock
from app.models.mb_member import MbMember, MbPointsLog
from app.models.od_order import OdOrder
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


def _seed(db, base: int, *, price: str = "10.00", total: int = 10, balance: int = 0) -> tuple[int, int]:
    """租户 1001 落商品/SKU/库存/会员，返回 (sku_id, member_id)。"""
    set_tenant(1001)
    try:
        gid, sid, mid = base + 1, base + 2, base + 3
        db.add(GdGoods(id=gid, tenant_id=1001, name="积分验收商品", type="NORMAL",
                       channel="NORMAL", status="ON_SALE", normal_on_sale=1))
        db.add(GdSku(id=sid, tenant_id=1001, goods_id=gid, sku_code=f"PT-{sid}", price=price))
        db.add(GdSkuStock(tenant_id=1001, goods_id=gid, sku_id=sid, channel="NORMAL",
                          total_stock=total, available_stock=total))
        db.add(MbMember(id=mid, tenant_id=1001, member_no=f"QAPT{mid}", points_balance=balance))
        db.commit()
        return sid, mid
    finally:
        reset()


def _buy(db, client, sku_id: int, mid: int, *, points_deduct: int | None = None) -> dict:
    h = _customer(mid, 1001)
    created = assert_biz_code(client.post("/api/c/order/create", headers=h, json={
        "skuId": sku_id, "quantity": 1, "deliveryType": "EXPRESS",
        "receiverName": "QA", "receiverPhone": "13800000000", "receiverAddress": "QA地址"}), BizCode.OK)["data"]
    oid = created["id"]
    payload = {} if points_deduct is None else {"pointsDeduct": points_deduct}
    paid = assert_biz_code(client.post(f"/api/c/order/{oid}/pay", headers=h, json=payload), BizCode.OK)["data"]
    return {"oid": oid, "paid": paid, "header": h}


def test_pay_with_points_deduct_reduces_amount_and_balance(client, db_session):
    base = 410_000_000 + int(uuid.uuid4().int % 10_000_000)
    sku_id, mid = _seed(db_session, base, price="10.00", balance=100)
    res = _buy(db_session, client, sku_id, mid, points_deduct=100)
    oid = res["oid"]

    assert res["paid"]["status"] == "PAID"
    assert res["paid"]["pointsDeducted"] == 100
    # 100 积分 / POINTS_REDUCE_RATIO(100) = 1.00 元抵扣
    assert Decimal(res["paid"]["payAmount"]) == Decimal("9.00")

    set_tenant(1001)
    try:
        m = db_session.query(MbMember).filter_by(id=mid, tenant_id=1001).one()
        # 初始 100 - 抵扣 100 + 发放 9(9.00*1) = 9
        assert m.points_balance == 9
        spend = db_session.query(MbPointsLog).filter_by(
            tenant_id=1001, member_id=mid, change_type="SPEND", ref_type="ORDER", ref_id=str(oid)).one()
        assert spend.amount == -100
        earn = db_session.query(MbPointsLog).filter_by(
            tenant_id=1001, member_id=mid, change_type="ORDER_EARN", ref_type="ORDER", ref_id=str(oid)).one()
        assert earn.amount == 9
    finally:
        reset()


def test_pay_rejected_when_points_insufficient(client, db_session):
    base = 420_000_000 + int(uuid.uuid4().int % 10_000_000)
    sku_id, mid = _seed(db_session, base, price="10.00", balance=50)
    h = _customer(mid, 1001)
    created = assert_biz_code(client.post("/api/c/order/create", headers=h, json={
        "skuId": sku_id, "quantity": 1, "deliveryType": "EXPRESS",
        "receiverName": "QA", "receiverPhone": "13800000000", "receiverAddress": "QA地址"}), BizCode.OK)["data"]
    oid = created["id"]
    # 想用 100 积分但只有 50
    body = client.post(f"/api/c/order/{oid}/pay", headers=h, json={"pointsDeduct": 100})
    assert body.status_code == 200
    assert body.json()["code"] == BizCode.POINTS_NOT_ENOUGH

    set_tenant(1001)
    try:
        o = db_session.query(OdOrder).filter_by(id=oid, tenant_id=1001).one()
        m = db_session.query(MbMember).filter_by(id=mid, tenant_id=1001).one()
        assert o.status == "PENDING_PAY" and m.points_balance == 50
        assert db_session.query(MbPointsLog).filter_by(tenant_id=1001, member_id=mid).count() == 0
    finally:
        reset()


def test_order_completion_earns_points_idempotently(client, db_session):
    base = 430_000_000 + int(uuid.uuid4().int % 10_000_000)
    sku_id, mid = _seed(db_session, base, price="10.00", balance=0)
    res = _buy(db_session, client, sku_id, mid)  # 不抵扣
    oid = res["oid"]

    set_tenant(1001)
    try:
        m = db_session.query(MbMember).filter_by(id=mid, tenant_id=1001).one()
        # 支付成功即发放：10.00 * POINTS_EARN_RATE(1) = 10
        assert m.points_balance == 10
    finally:
        reset()

    # 先发货再确认收货，验证幂等：不应重复发放
    assert_biz_code(client.post(f"/api/mc/order/{oid}/ship", headers=_merchant(1001),
                                json={"expressCompany": "SF", "expressNo": "SF123"}), BizCode.OK)
    assert_biz_code(client.post(f"/api/c/order/{oid}/confirm-receive", headers=res["header"]), BizCode.OK)
    set_tenant(1001)
    try:
        m = db_session.query(MbMember).filter_by(id=mid, tenant_id=1001).one()
        assert m.points_balance == 10
        earns = db_session.query(MbPointsLog).filter_by(
            tenant_id=1001, member_id=mid, change_type="ORDER_EARN", ref_type="ORDER", ref_id=str(oid)).all()
        assert len(earns) == 1 and earns[0].amount == 10
        o = db_session.query(OdOrder).filter_by(id=oid, tenant_id=1001).one()
        assert o.earned_points == 10
    finally:
        reset()


def test_refund_approve_reverses_earned_points(client, db_session):
    base = 440_000_000 + int(uuid.uuid4().int % 10_000_000)
    sku_id, mid = _seed(db_session, base, price="10.00", balance=0)
    res = _buy(db_session, client, sku_id, mid)
    oid = res["oid"]

    # 申请退款
    refund = assert_biz_code(client.post("/api/c/refund", headers=res["header"],
                                         json={"orderId": oid, "reasonCode": "QA", "reasonDesc": "验收"}), BizCode.OK)["data"]
    rid = refund["id"]
    # 商家通过
    assert_biz_code(client.post(f"/api/mc/refund/{rid}/approve", headers=_merchant(1001)), BizCode.OK)

    set_tenant(1001)
    try:
        m = db_session.query(MbMember).filter_by(id=mid, tenant_id=1001).one()
        # 发放 10 后被回退 → 0
        assert m.points_balance == 0
        rollback = db_session.query(MbPointsLog).filter_by(
            tenant_id=1001, member_id=mid, change_type="REFUND_ROLLBACK", ref_type="REFUND", ref_id=str(rid)).one()
        assert rollback.amount == -10
        # 原发放流水仍在（审计）
        earn = db_session.query(MbPointsLog).filter_by(
            tenant_id=1001, member_id=mid, change_type="ORDER_EARN", ref_type="ORDER", ref_id=str(oid)).one()
        assert earn.amount == 10
    finally:
        reset()
