"""退款驳回须按申请前的订单原始状态精确还原（而非一律 PAID）。"""
from __future__ import annotations

import uuid
from decimal import Decimal

from app.core.errors import BizCode
from app.core.security import SCOPE_CUSTOMER, SCOPE_MERCHANT, create_access_token
from app.core.tenant_context import reset, set_tenant
from app.models.mb_member import MbMember
from app.models.od_order import OdOrder, OdOrderItem, OdRefund
from tests.conftest import assert_biz_code


def _enable_sqlite_autoincrement() -> None:
    from sqlalchemy import BigInteger, Integer
    from app.db.base import Base
    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if isinstance(col.type, BigInteger):
                col.type = BigInteger().with_variant(Integer(), "sqlite")


_enable_sqlite_autoincrement()


def _cust(member_id: int, tenant_id: int) -> dict[str, str]:
    return {"Authorization": "Bearer " + create_access_token(str(member_id), SCOPE_CUSTOMER, tenant_id=tenant_id)}


def _merch(tenant_id: int) -> dict[str, str]:
    return {"Authorization": "Bearer " + create_access_token("10", SCOPE_MERCHANT, tenant_id=tenant_id, perms=["MC_ALL"])}


def _order(db, tenant_id: int, member_id: int, oid: int, status: str) -> None:
    db.add(OdOrder(id=oid, tenant_id=tenant_id, order_no=f"QA-RR-{oid}", channel="NORMAL", member_id=member_id,
                   status=status, delivery_type="EXPRESS", goods_amount=Decimal("9.90"), pay_amount=Decimal("9.90")))
    db.add(OdOrderItem(tenant_id=tenant_id, order_id=oid, goods_id=oid, sku_id=oid,
                       channel="NORMAL", goods_name="QA退款商品", goods_type="PHYSICAL",
                       price=Decimal("9.90"), quantity=1, subtotal_amount=Decimal("9.90")))


def _apply_and_reject(client, db_session, member_id: int, tenant_id: int, oid: int, expect_status: str) -> None:
    set_tenant(tenant_id)
    try:
        o = db_session.query(OdOrder).filter_by(id=oid, tenant_id=tenant_id).one()
        assert o.status == expect_status
    finally:
        reset()
    db_session.expire_all()
    body = assert_biz_code(client.post("/api/c/refund", headers=_cust(member_id, tenant_id),
                                       json={"orderId": oid, "reasonCode": "OTHER", "reasonDesc": "QA"}), BizCode.OK)["data"]
    refund_id = body["id"]
    set_tenant(tenant_id)
    try:
        r = db_session.query(OdRefund).filter_by(id=refund_id, tenant_id=tenant_id).one()
        assert r.order_status_before == expect_status
        assert db_session.query(OdOrder).filter_by(id=oid, tenant_id=tenant_id).one().status == "REFUNDING"
    finally:
        reset()
    assert_biz_code(client.post(f"/api/mc/refund/{refund_id}/reject", headers=_merch(tenant_id),
                                json={"rejectReason": "QA驳回"}), BizCode.OK)
    db_session.expire_all()
    set_tenant(tenant_id)
    try:
        r = db_session.query(OdRefund).filter_by(id=refund_id, tenant_id=tenant_id).one()
        o = db_session.query(OdOrder).filter_by(id=oid, tenant_id=tenant_id).one()
        assert r.status == "REJECTED"
        assert o.status == expect_status, f"驳回后订单状态应为 {expect_status}，实际 {o.status}"
    finally:
        reset()


def test_reject_restores_shipped(client, db_session):
    base = 70_000_000 + int(uuid.uuid4().int % 10_000_000)
    member, oid = base, base + 100
    set_tenant(1001)
    try:
        db_session.add(MbMember(id=member, tenant_id=1001, member_no=f"QARR{member}"))
        _order(db_session, 1001, member, oid, "SHIPPED")
        db_session.commit()
    finally:
        reset()
    _apply_and_reject(client, db_session, member, 1001, oid, "SHIPPED")


def test_reject_restores_pending_receive(client, db_session):
    base = 71_000_000 + int(uuid.uuid4().int % 10_000_000)
    member, oid = base, base + 100
    set_tenant(1001)
    try:
        db_session.add(MbMember(id=member, tenant_id=1001, member_no=f"QARR{member}"))
        _order(db_session, 1001, member, oid, "PENDING_RECEIVE")
        db_session.commit()
    finally:
        reset()
    _apply_and_reject(client, db_session, member, 1001, oid, "PENDING_RECEIVE")


def test_reject_restores_paid_compat(client, db_session):
    base = 72_000_000 + int(uuid.uuid4().int % 10_000_000)
    member, oid = base, base + 100
    set_tenant(1001)
    try:
        db_session.add(MbMember(id=member, tenant_id=1001, member_no=f"QARR{member}"))
        _order(db_session, 1001, member, oid, "PAID")
        db_session.commit()
    finally:
        reset()
    _apply_and_reject(client, db_session, member, 1001, oid, "PAID")
