"""商家退款审核状态机、积分回滚与跨租户验收。"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import uuid

from app.core.errors import BizCode
from app.core.security import SCOPE_MERCHANT, create_access_token
from app.core.tenant_context import reset, set_tenant
from app.models.mb_member import MbMember, MbPointsLog
from app.models.od_order import OdOrder, OdRefund, OdVerifyCode
from tests.conftest import assert_biz_code


def _enable_sqlite_autoincrement() -> None:
    from sqlalchemy import BigInteger, Integer
    from app.db.base import Base
    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if isinstance(col.type, BigInteger):
                col.type = BigInteger().with_variant(Integer(), "sqlite")


_enable_sqlite_autoincrement()


def _merchant(tenant_id: int) -> dict[str, str]:
    return {"Authorization": "Bearer " + create_access_token("10", SCOPE_MERCHANT, tenant_id=tenant_id, perms=["MC_ALL"])}


def _order(db, tid: int, mid: int, oid: int, status: str = "REFUNDING") -> None:
    db.add(OdOrder(id=oid, tenant_id=tid, order_no=f"QA-MR-{oid}", channel="NORMAL", member_id=mid,
                   status=status, delivery_type="VERIFY", goods_amount=Decimal("9.90"), pay_amount=Decimal("9.90")))


def test_mc_refund_audit_state_machine_and_side_effects(client, db_session):
    base = 80_000_000 + int(uuid.uuid4().int % 10_000_000)
    member, member_b = base, base + 1
    approve_order, reject_order, other_order = base + 10, base + 11, base + 12
    set_tenant(1001)
    try:
        db_session.add(MbMember(id=member, tenant_id=1001, member_no=f"QAM{member}", points_balance=3, points_total_earn=5))
        _order(db_session, 1001, member, approve_order)
        _order(db_session, 1001, member, reject_order)
        db_session.add_all([
            OdRefund(tenant_id=1001, order_id=approve_order, refund_no=f"QAAPP{base}", refund_amount=Decimal("9.90"), reason_code="QA", status="PENDING_AUDIT"),
            OdRefund(tenant_id=1001, order_id=reject_order, refund_no=f"QAREJ{base}", refund_amount=Decimal("9.90"), reason_code="QA", status="PENDING_AUDIT"),
            MbPointsLog(tenant_id=1001, member_id=member, amount=5, balance_after=3, change_type="ORDER_EARN", ref_type="ORDER", ref_id=str(approve_order)),
            OdVerifyCode(tenant_id=1001, order_id=approve_order, order_item_id=approve_order, member_id=member, code=f"QA-HX-{base}", code_type="VERIFY", goods_name="QA券", valid_start=datetime.now()-timedelta(days=1), valid_end=datetime.now()+timedelta(days=1), status="UNUSED"),
        ])
        db_session.commit()
        refund_approve = db_session.query(OdRefund).filter_by(order_id=approve_order, tenant_id=1001).one().id
        refund_reject = db_session.query(OdRefund).filter_by(order_id=reject_order, tenant_id=1001).one().id
    finally:
        reset()
    set_tenant(2002)
    try:
        db_session.add(MbMember(id=member_b, tenant_id=2002, member_no=f"QAM{member_b}")); _order(db_session, 2002, member_b, other_order)
        db_session.add(OdRefund(tenant_id=2002, order_id=other_order, refund_no=f"QAOT{base}", refund_amount=Decimal("9.90"), reason_code="QA", status="PENDING_AUDIT"))
        db_session.commit(); refund_other = db_session.query(OdRefund).filter_by(order_id=other_order, tenant_id=2002).one().id
    finally:
        reset()
    ha, hb = _merchant(1001), _merchant(2002)

    assert_biz_code(client.post(f"/api/mc/refund/{refund_approve}/approve", headers=ha), BizCode.OK)
    assert_biz_code(client.post(f"/api/mc/refund/{refund_approve}/approve", headers=ha), BizCode.CONFLICT)
    assert_biz_code(client.post(f"/api/mc/refund/{refund_approve}/reject", headers=ha, json={"rejectReason": "反向"}), BizCode.CONFLICT)
    set_tenant(1001)
    try:
        r = db_session.query(OdRefund).filter_by(id=refund_approve, tenant_id=1001).one()
        o = db_session.query(OdOrder).filter_by(id=approve_order, tenant_id=1001).one()
        m = db_session.query(MbMember).filter_by(id=member, tenant_id=1001).one()
        v = db_session.query(OdVerifyCode).filter_by(order_id=approve_order, tenant_id=1001).one()
        logs = db_session.query(MbPointsLog).filter_by(tenant_id=1001, member_id=member, ref_type="REFUND", ref_id=str(refund_approve)).all()
        assert r.status == "APPROVED" and r.wx_refund_id == "FAKE-" + r.refund_no and r.rollback_points == 3 and r.rollback_debt == 2
        assert o.status == "REFUNDED" and v.status == "REFUNDED" and m.points_balance == 0 and m.points_debt == 2
        assert len(logs) == 1 and logs[0].amount == -3 and logs[0].balance_after == 0
    finally:
        reset()
    assert_biz_code(client.post(f"/api/mc/refund/{refund_reject}/reject", headers=ha, json={"rejectReason": "QA驳回"}), BizCode.OK)
    set_tenant(1001)
    try:
        r = db_session.query(OdRefund).filter_by(id=refund_reject, tenant_id=1001).one(); o = db_session.query(OdOrder).filter_by(id=reject_order, tenant_id=1001).one()
        assert r.status == "REJECTED" and r.reject_reason == "QA驳回" and o.status == "PAID"
    finally:
        reset()
    assert_biz_code(client.post(f"/api/mc/refund/{refund_other}/approve", headers=ha), BizCode.NOT_FOUND)
    assert_biz_code(client.post(f"/api/mc/refund/{refund_approve}/approve", headers=hb), BizCode.NOT_FOUND)
