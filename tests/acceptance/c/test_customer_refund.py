"""用户退款申请/详情的状态机、隔离和持久化验收。"""
from __future__ import annotations

import uuid
from decimal import Decimal

from app.core.errors import BizCode
from app.core.security import SCOPE_CUSTOMER, create_access_token
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


def _header(member_id: int, tenant_id: int) -> dict[str, str]:
    return {"Authorization": "Bearer " + create_access_token(str(member_id), SCOPE_CUSTOMER, tenant_id=tenant_id)}


def _order(db, tenant_id: int, member_id: int, oid: int, status: str, goods_type: str = "PHYSICAL") -> None:
    db.add(OdOrder(id=oid, tenant_id=tenant_id, order_no=f"QA-RF-{oid}", channel="NORMAL", member_id=member_id,
                   status=status, delivery_type="EXPRESS", goods_amount=Decimal("9.90"), pay_amount=Decimal("9.90")))
    db.add(OdOrderItem(tenant_id=tenant_id, order_id=oid, goods_id=oid, sku_id=oid,
                       channel="NORMAL", goods_name="QA退款商品", goods_type=goods_type,
                       price=Decimal("9.90"), quantity=1, subtotal_amount=Decimal("9.90")))


def test_customer_refund_state_machine_detail_and_isolation(client, db_session):
    base = 60_000_000 + int(uuid.uuid4().int % 10_000_000)
    member_a, member_b, member_c = base, base + 1, base + 2
    paid, pending, virtual, other_member, other_tenant = base + 100, base + 101, base + 102, base + 103, base + 104
    set_tenant(1001)
    try:
        db_session.add_all([MbMember(id=member_a, tenant_id=1001, member_no=f"QAR{member_a}"), MbMember(id=member_b, tenant_id=1001, member_no=f"QAR{member_b}")])
        _order(db_session, 1001, member_a, paid, "PAID")
        _order(db_session, 1001, member_a, pending, "PENDING_PAY")
        _order(db_session, 1001, member_a, virtual, "PAID", "VIRTUAL")
        _order(db_session, 1001, member_b, other_member, "PAID")
        db_session.commit()
    finally:
        reset()
    set_tenant(2002)
    try:
        db_session.add(MbMember(id=member_c, tenant_id=2002, member_no=f"QAR{member_c}"))
        _order(db_session, 2002, member_c, other_tenant, "PAID")
        db_session.commit()
    finally:
        reset()
    ha, hb, hc = _header(member_a, 1001), _header(member_b, 1001), _header(member_c, 2002)

    assert_biz_code(client.post("/api/c/refund", headers=ha, json={"reasonCode": "ONLY"}), BizCode.PARAM_ERROR)
    body = assert_biz_code(client.post("/api/c/refund", headers=ha, json={"orderId": paid, "reasonCode": "OTHER", "reasonDesc": "QA原因"}), BizCode.OK)["data"]
    refund_id = body["id"]
    detail = assert_biz_code(client.get(f"/api/c/refund/{refund_id}", headers=ha), BizCode.OK)["data"]
    assert detail["orderId"] == paid and detail["refundAmount"] == "9.90" and detail["reasonDesc"] == "QA原因"
    set_tenant(1001)
    try:
        assert db_session.query(OdRefund).filter_by(id=refund_id, tenant_id=1001, order_id=paid).one().reason_desc == "QA原因"
    finally:
        reset()
    assert_biz_code(client.post("/api/c/refund", headers=ha, json={"orderId": paid, "reasonCode": "OTHER", "reasonDesc": "重复"}), BizCode.REFUND_DUPLICATE)
    assert_biz_code(client.post("/api/c/refund", headers=ha, json={"orderId": virtual, "reasonCode": "OTHER", "reasonDesc": "虚拟"}), BizCode.VIRTUAL_REFUND_FORBIDDEN)
    assert_biz_code(client.post("/api/c/refund", headers=ha, json={"orderId": pending, "reasonCode": "OTHER", "reasonDesc": "未支付"}), BizCode.ORDER_STATUS_INVALID)
    assert_biz_code(client.post("/api/c/refund", headers=hb, json={"orderId": paid, "reasonCode": "OTHER", "reasonDesc": "越权"}), BizCode.NOT_FOUND)
    assert_biz_code(client.post("/api/c/refund", headers=ha, json={"orderId": other_tenant, "reasonCode": "OTHER", "reasonDesc": "跨租户"}), BizCode.NOT_FOUND)
    assert_biz_code(client.get(f"/api/c/refund/{refund_id}", headers=hb), BizCode.NOT_FOUND)
    assert_biz_code(client.get(f"/api/c/refund/{refund_id}", headers=hc), BizCode.NOT_FOUND)
