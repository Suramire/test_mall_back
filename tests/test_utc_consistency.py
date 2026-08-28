"""时间字段统一 UTC 回归：写入口径（deleted_at/last_login_at/joined_at/cancelled_at）
与 API 输出格式（ISO-8601 带 Z 后缀）。
"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone

from app.core.errors import BizCode
from app.core.security import (
    SCOPE_CUSTOMER,
    SCOPE_MERCHANT,
    SCOPE_PLATFORM,
    create_access_token,
    hash_password,
)
from app.core.tenant_context import reset, set_tenant
from app.models.gd_goods import GdGoods
from app.models.mb_member import MbMember
from app.models.mc_config import McStore
from app.models.od_order import OdOrder, OdOrderItem
from app.models.pf_role import PfRole
from app.models.pf_staff import PfStaff
from app.models.pf_tenant import PfTenant
from app.services.goods import delete_goods
from app.services.platform_auth import login as platform_login
from tests.conftest import assert_biz_code


def _enable_sqlite_autoincrement() -> None:
    from sqlalchemy import BigInteger, Integer

    from app.db.base import Base

    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if isinstance(col.type, BigInteger):
                col.type = BigInteger().with_variant(Integer(), "sqlite")


_enable_sqlite_autoincrement()


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _assert_written_as_utc(ts: datetime | None) -> None:
    """断言：已赋值、无 tzinfo、且等于当前 UTC 时刻（±60s）。TZ≠UTC 环境下可区分本地写入。"""
    assert ts is not None, "时间字段未写入"
    assert ts.tzinfo is None, f"应为 naive UTC，实际 {ts!r}"
    assert abs((_utcnow_naive() - ts).total_seconds()) < 60, f"非 UTC 口径: {ts!r}"


def _merchant_headers() -> dict[str, str]:
    token = create_access_token("10", SCOPE_MERCHANT, tenant_id=1001, perms=["MC_ALL"])
    return {"Authorization": f"Bearer {token}"}


def _customer_headers(member_id: int, tenant_id: int = 1001) -> dict[str, str]:
    token = create_access_token(str(member_id), SCOPE_CUSTOMER, tenant_id=tenant_id)
    return {"Authorization": f"Bearer {token}"}


def test_store_delete_writes_utc_deleted_at(client, db_session):
    r = client.post("/api/mc/store", headers=_merchant_headers(), json={"name": "QA门店"})
    store_id = assert_biz_code(r, BizCode.OK)["data"]["id"]
    assert_biz_code(client.delete(f"/api/mc/store/{store_id}", headers=_merchant_headers()), BizCode.OK)
    set_tenant(1001)
    try:
        row = db_session.get(McStore, store_id)
    finally:
        reset()
    _assert_written_as_utc(row.deleted_at)


def test_goods_delete_writes_utc_deleted_at(client, db_session):
    set_tenant(1001)
    try:
        g = GdGoods(tenant_id=1001, name="QA软删商品", type="PHYSICAL", channel="NORMAL")
        db_session.add(g)
        db_session.commit()
        gid = g.id
        delete_goods(db_session, gid)
        db_session.refresh(g)
        _assert_written_as_utc(g.deleted_at)
    finally:
        reset()


def test_role_delete_writes_utc_deleted_at(client, db_session):
    role = PfRole(name=f"qa-utc-{uuid.uuid4().hex[:8]}", perms=[], is_system=0)
    db_session.add(role)
    db_session.commit()
    rid = role.id
    headers = {"Authorization": "Bearer " + create_access_token(
        "1", SCOPE_PLATFORM,
        perms=["PF_DASHBOARD", "PF_MERCHANT_LIST", "PF_MERCHANT_EDIT",
               "PF_MERCHANT_STATUS", "PF_MERCHANT_IMPERSONATE", "PF_ROLE", "PF_STAFF"],
    )}
    resp = client.delete(f"/api/pf/role/{rid}", headers=headers)
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    row = db_session.get(PfRole, rid)
    _assert_written_as_utc(row.deleted_at)


def test_platform_login_writes_utc_last_login_at(db_session):
    role = PfRole(name=f"qa-login-{uuid.uuid4().hex[:8]}", perms=[], is_system=0)
    db_session.add(role)
    db_session.flush()
    account = f"qa-utc-{uuid.uuid4().hex[:10]}"
    staff = PfStaff(account=account, name="QA登录", password_hash=hash_password("Passw0rd!123"),
                    role_id=role.id, status="ENABLED")
    db_session.add(staff)
    db_session.commit()
    platform_login(db_session, account, "Passw0rd!123", ip="127.0.0.1")
    db_session.commit()
    db_session.refresh(staff)
    _assert_written_as_utc(staff.last_login_at)


def test_customer_register_writes_utc_joined_at(client, db_session):
    db_session.add(PfTenant(id=1, tenant_no="M10001", name="QA租户", status="NORMAL"))
    db_session.commit()
    phone = f"138{uuid.uuid4().int % 100000000:08d}"
    r = client.post("/api/c/auth/login", json={"phone": phone})
    assert_biz_code(r, BizCode.OK)
    phone_hash = hashlib.sha256(phone.encode()).hexdigest()
    set_tenant(1)
    try:
        m = db_session.query(MbMember).filter_by(phone_hash=phone_hash).one()
        _assert_written_as_utc(m.joined_at)
    finally:
        reset()


def test_order_cancel_persists_cancelled_at(client, db_session):
    suffix = uuid.uuid4().hex[:12]
    set_tenant(1001)
    try:
        m = MbMember(tenant_id=1001, member_no=f"QAU{suffix}", nickname="取消QA")
        db_session.add(m)
        db_session.flush()
        o = OdOrder(tenant_id=1001, order_no=f"ORDQA{suffix}", channel="NORMAL",
                    member_id=m.id, status="PENDING_PAY", delivery_type="EXPRESS")
        db_session.add(o)
        db_session.flush()
        db_session.add(OdOrderItem(tenant_id=1001, order_id=o.id, goods_id=990001, sku_id=990002,
                                   channel="NORMAL", goods_name="QA商品", goods_type="PHYSICAL", quantity=1))
        db_session.commit()
        oid = o.id
        mid = m.id
    finally:
        reset()
    assert_biz_code(client.post(f"/api/c/order/{oid}/cancel", headers=_customer_headers(mid)), BizCode.OK)
    set_tenant(1001)
    try:
        db_session.expire_all()
        row = db_session.get(OdOrder, oid)
        assert row.status == "CLOSED"
        _assert_written_as_utc(row.cancelled_at)
    finally:
        reset()


def test_api_datetime_output_ends_with_z(client, db_session):
    suffix = uuid.uuid4().hex[:12]
    set_tenant(1001)
    try:
        m = MbMember(tenant_id=1001, member_no=f"QAS{suffix}", nickname="序列化QA")
        db_session.add(m)
        db_session.commit()
        mid = m.id
    finally:
        reset()
    assert_biz_code(client.post("/api/mc/points/adjust", headers=_merchant_headers(), json={
        "memberId": mid, "points": 5, "remark": "QA序列化", "idempotencyKey": f"utc-z-{suffix}",
    }), BizCode.OK)
    body = client.get(f"/api/mc/member/{mid}/points-log", headers=_merchant_headers()).json()["data"]
    created = body[0]["createdAt"]
    assert created.endswith("Z"), f"输出应带 Z 后缀: {created}"
    assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$", created), created
