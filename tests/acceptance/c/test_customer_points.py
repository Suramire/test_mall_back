"""用户积分摘要/流水：真实商家加分后的隔离、分页与 UTC 输出验收。"""
from __future__ import annotations

import uuid

from app.core.errors import BizCode
from app.core.security import SCOPE_CUSTOMER, SCOPE_MERCHANT, create_access_token
from app.core.tenant_context import reset, set_tenant
from app.models.mb_member import MbMember
from tests.conftest import assert_biz_code


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


def _merchant(tenant_id: int) -> dict[str, str]:
    return {"Authorization": "Bearer " + create_access_token("10", SCOPE_MERCHANT, tenant_id=tenant_id, perms=["MC_ALL"])}


def test_customer_points_summary_log_paging_and_isolation(client, db_session):
    member_a = 50_000_000 + int(uuid.uuid4().int % 10_000_000)
    member_b = member_a + 10_000_000
    set_tenant(1001)
    try:
        db_session.add(MbMember(id=member_a, tenant_id=1001, member_no=f"QAP{member_a}", nickname="积分A"))
        db_session.commit()
    finally:
        reset()
    set_tenant(2002)
    try:
        db_session.add(MbMember(id=member_b, tenant_id=2002, member_no=f"QAP{member_b}", nickname="积分B"))
        db_session.commit()
    finally:
        reset()

    ca, cb = _customer(member_a, 1001), _customer(member_b, 2002)
    ma = _merchant(1001)
    empty = assert_biz_code(client.get("/api/c/points/log", headers=cb), BizCode.OK)["data"]
    assert empty["total"] == 0 and empty["list"] == []

    for amount, key in ((7, "qa-cpoints-1"), (3, "qa-cpoints-2")):
        assert_biz_code(client.post("/api/mc/points/adjust", headers=ma, json={
            "memberId": member_a, "points": amount, "remark": "QA用户积分", "idempotencyKey": key,
        }), BizCode.OK)
    summary = assert_biz_code(client.get("/api/c/points/summary", headers=ca), BizCode.OK)["data"]
    assert summary["pointsBalance"] == 10 and summary["totalEarn"] == 10 and summary["totalUsed"] == 0
    log = assert_biz_code(client.get("/api/c/points/log?page=1&size=1", headers=ca), BizCode.OK)["data"]
    assert log["total"] == 2 and len(log["list"]) == 1
    assert log["list"][0]["amount"] == 3 and log["list"][0]["createdAt"].endswith("Z")
    assert_biz_code(client.get("/api/c/points/log?type=MANUAL_ADJUST", headers=ca), BizCode.OK)
    # Token 的 member/tenant 由服务端上下文固定，另一个用户只能读自己的空态。
    cross = assert_biz_code(client.get("/api/c/points/log?page=1&size=20", headers=cb), BizCode.OK)["data"]
    assert cross["total"] == 0 and cross["list"] == []
