"""商家小程序独立 /api/mp 契约验收：登录→身份→工作台→核销→记录 全链路。

覆盖：
- POST /api/mp/auth/merchant-login（复用 mc_auth 账密体系签发 token）
- GET  /api/mp/me/profile
- GET  /api/mp/workbench/kpi、GET /api/mp/workbench/todo
- GET  /api/mp/verify/query、POST /api/mp/verify、GET /api/mp/verify/log|records
- GET  /api/mp/points/records
- 权限负例：未登录(业务码40100)、用户端token打商家身份(40301)、跨租户数据隔离。

契约说明：本项目 API 信封为 {code,message,data,traceId}，业务错误 HTTP 恒 200，
"未登录401"按契约断言业务码 40100（与 mc 验收口径一致）。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import assert_ok


def _enable_sqlite_autoincrement() -> None:
    """让 BigInteger 主键在 SQLite 下自增（方言差异补丁，见 acceptance/mc/conftest.py）。"""
    from sqlalchemy import BigInteger, Integer

    from app.db.base import Base

    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if not isinstance(col.type, BigInteger):
                continue
            col.type = BigInteger().with_variant(Integer(), "sqlite")


# 必须在 import 期执行（engine fixture 建表前），与 mc 夹具同口径。
_enable_sqlite_autoincrement()

TENANT_A = 1
TENANT_B = 2002
STAFF_A_ACCOUNT = "shopA"
STAFF_A_PASSWORD = "Secret123"
STAFF_B_ACCOUNT = "shopB"
STAFF_B_PASSWORD = "Secret456"

UNUSED_CODE_A = "HX-MP-A-UNUSED"
USED_CODE_A = "HX-MP-A-USED"

_DIRTY_TABLES = (
    "mc_staff", "gd_goods", "gd_sku", "gd_sku_stock", "mb_member",
    "mb_points_log", "od_order", "od_order_item", "od_verify_code",
    "mc_store", "mc_shop", "mc_notice",
)


def _truncate(engine) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        for table in _DIRTY_TABLES:
            try:
                conn.execute(text(f"DELETE FROM {table}"))
            except Exception:  # noqa: BLE001,S110
                pass


def _now_iso(offset_days: int = 0) -> datetime:
    """与 mall.py _day_range 同口径的 UTC naive now。"""
    return datetime.now(UTC).replace(tzinfo=None) + timedelta(days=offset_days)


def _mk_staff(tenant_id: int, sid: int, account: str, password: str):
    from app.core.security import hash_password
    from app.models.mc_staff import McStaff

    return McStaff(
        id=sid, tenant_id=tenant_id, account=account,
        name=f"店主{tenant_id}", password_hash=hash_password(password),
        phone="13800001024", is_admin=1, status="ENABLED", pwd_reset_required=0,
    )


def _seed_order(s, tid: int, oid: int, mid: int, status: str, pay_amount: str, created_at):
    from app.models.od_order import OdOrder, OdOrderItem

    s.add(OdOrder(tenant_id=tid, id=oid, order_no=f"ORDMP{oid}",
                  channel="NORMAL", member_id=mid, member_no=f"M{mid}",
                  status=status, delivery_type="EXPRESS",
                  goods_amount=pay_amount, pay_amount=pay_amount,
                  created_at=created_at, paid_at=created_at))
    s.add(OdOrderItem(tenant_id=tid, order_id=oid, goods_id=101, sku_id=1,
                      channel="NORMAL", goods_name="A租户商品",
                      goods_type="NORMAL", spec_text="", price=pay_amount,
                      quantity=1, subtotal_amount=pay_amount))


def _mk_verify(s, tid: int, oid: int, item_id: int, mid: int, code: str,
               status: str, start_off: int, end_off: int, verified_at=None):
    from app.models.od_order import OdVerifyCode

    s.add(OdVerifyCode(tenant_id=tid, order_id=oid, order_item_id=item_id,
                       member_id=mid, code=code, code_type="VERIFY",
                       goods_name="A租户商品",
                       valid_start=_now_iso(start_off), valid_end=_now_iso(end_off),
                       status=status, verified_at=verified_at))


@pytest.fixture
def mp_seed(app, engine):
    """两个租户的员工/订单/核销码/积分流水种子，用于 mp 契约与越权验收。"""
    from sqlalchemy.orm import sessionmaker

    from app.core.tenant_context import reset, set_tenant
    from app.models.mb_member import MbMember, MbPointsLog
    from app.models.mc_config import McStore

    _truncate(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = Session()
    try:
        set_tenant(TENANT_A)
        s.add(_mk_staff(TENANT_A, 10, STAFF_A_ACCOUNT, STAFF_A_PASSWORD))
        s.add(MbMember(id=201, tenant_id=TENANT_A, member_no="MA0001",
                       nickname="A会员", phone_mask="138****1111",
                       points_balance=100, joined_at=_now_iso(0)))
        s.add(MbPointsLog(tenant_id=TENANT_A, member_id=201, amount=10,
                          balance_after=100, change_type="MANUAL_ADJUST",
                          ref_type="MANUAL", ref_id="mp-seed-log-1",
                          remark="mp-seed", operator_id=10))
        s.add(MbPointsLog(tenant_id=TENANT_A, member_id=201, amount=-5,
                          balance_after=95, change_type="MANUAL_ADJUST",
                          ref_type="MANUAL", ref_id="mp-seed-log-2",
                          remark="mp-seed-use", operator_id=10))
        _seed_order(s, TENANT_A, 1001, 201, "PAID", "12.50", _now_iso(-3))
        _seed_order(s, TENANT_A, 1003, 201, "PAID", "12.50", _now_iso(0))
        _mk_verify(s, TENANT_A, 1003, 2, 201, UNUSED_CODE_A, "UNUSED", -5, 5)
        _mk_verify(s, TENANT_A, 1001, 1, 201, USED_CODE_A, "USED", -5, 5,
                   verified_at=_now_iso(0))
        s.add(McStore(tenant_id=TENANT_A, name="A旗舰门店"))
        s.commit()

        set_tenant(TENANT_B)
        s.add(_mk_staff(TENANT_B, 20, STAFF_B_ACCOUNT, STAFF_B_PASSWORD))
        s.commit()
        yield {"staff_a": 10, "staff_b": 20, "member_a": 201}
    finally:
        s.close()
        _truncate(engine)
        reset()


@pytest.fixture
def auth_a(client, mp_seed) -> dict:
    """租户 A 商家：走真实登录接口换取 token。"""
    resp = client.post("/api/mp/auth/merchant-login",
                       json={"account": STAFF_A_ACCOUNT, "password": STAFF_A_PASSWORD})
    body = resp.json()
    assert resp.status_code == 200 and body["code"] == 0, body
    return body["data"]


@pytest.fixture
def headers_a(auth_a) -> dict:
    return {"Authorization": f"Bearer {auth_a['accessToken']}"}


@pytest.fixture
def headers_b(client, mp_seed) -> dict:
    resp = client.post("/api/mp/auth/merchant-login",
                       json={"account": STAFF_B_ACCOUNT, "password": STAFF_B_PASSWORD})
    body = resp.json()
    assert resp.status_code == 200 and body["code"] == 0, body
    return {"Authorization": f"Bearer {body['data']['accessToken']}"}


@pytest.fixture
def customer_headers() -> dict:
    from app.core.security import SCOPE_CUSTOMER, create_access_token

    token = create_access_token(subject="201", scope=SCOPE_CUSTOMER,
                                tenant_id=TENANT_A, perms=[])
    return {"Authorization": f"Bearer {token}"}


def test_mp_login_and_profile_chain(client, mp_seed, auth_a, headers_a):
    assert auth_a["staff"]["account"] == STAFF_A_ACCOUNT
    assert auth_a["tenant"]["id"] == TENANT_A
    assert auth_a["perms"] == ["MC_ALL"]

    resp = client.get("/api/mp/me/profile", headers=headers_a)
    body = resp.json()
    assert resp.status_code == 200 and body["code"] == 0, body
    assert body["data"]["staff"]["id"] == 10
    assert body["data"]["staff"]["account"] == STAFF_A_ACCOUNT
    assert body["data"]["tenant"]["id"] == TENANT_A
    assert isinstance(body["data"]["perms"], list)


def test_mp_login_wrong_password(client, mp_seed):
    resp = client.post("/api/mp/auth/merchant-login",
                       json={"account": STAFF_A_ACCOUNT, "password": "wrong"})
    body = resp.json()
    assert resp.status_code == 200 and body["code"] != 0


def test_mp_workbench_kpi_and_todo(client, mp_seed, headers_a):
    kpi = client.get("/api/mp/workbench/kpi", headers=headers_a).json()
    assert kpi["code"] == 0
    data = kpi["data"]
    for key in ("todayOrders", "todaySales", "newMembers", "todayVerify"):
        assert key in data, key
        assert "value" in data[key] and "delta" in data[key]
    assert data["todayOrders"]["value"] >= 1
    assert data["todayVerify"]["value"] >= 1

    todo = client.get("/api/mp/workbench/todo", headers=headers_a).json()
    assert todo["code"] == 0
    for key in ("pendingShip", "pendingRefund", "pendingVerify", "pendingPickup"):
        assert key in todo["data"], key
    assert todo["data"]["pendingVerify"] >= 1


def test_mp_verify_query_confirm_and_records(client, mp_seed, headers_a):
    q = client.get("/api/mp/verify/query", params={"code": UNUSED_CODE_A},
                   headers=headers_a).json()
    assert q["code"] == 0 and q["data"]["status"] == "UNUSED"
    assert q["data"]["goodsName"] == "A租户商品"

    v = client.post("/api/mp/verify", json={"code": UNUSED_CODE_A},
                    headers=headers_a).json()
    assert v["code"] == 0, v
    assert v["data"]["status"] == "USED"

    again = client.post("/api/mp/verify", json={"code": UNUSED_CODE_A},
                        headers=headers_a).json()
    assert again["code"] == 44002

    log = client.get("/api/mp/verify/log", headers=headers_a).json()
    assert log["code"] == 0
    by_code = {x["code"]: x for x in log["data"]}
    assert by_code[UNUSED_CODE_A]["status"] == "USED"
    assert by_code[UNUSED_CODE_A]["verifiedAt"]
    assert by_code[USED_CODE_A]["status"] == "USED"

    records = client.get("/api/mp/verify/records", headers=headers_a).json()
    assert records["code"] == 0
    rec_by_code = {x["code"]: x for x in records["data"]}
    assert rec_by_code[UNUSED_CODE_A]["status"] == "USED"


def test_mp_points_records(client, mp_seed, headers_a):
    resp = client.get("/api/mp/points/records", headers=headers_a)
    body = resp.json()
    assert resp.status_code == 200 and body["code"] == 0, body
    assert body["data"]["total"] >= 2
    rows = body["data"]["list"]
    assert all(r["memberId"] == 201 for r in rows)
    assert any(r["amount"] == 10 for r in rows)
    assert any(r["changeType"] == "MANUAL_ADJUST" for r in rows)

    filtered = client.get("/api/mp/points/records",
                          params={"memberId": 999999}, headers=headers_a).json()
    assert filtered["code"] == 0 and filtered["data"]["total"] == 0


def test_mp_unauthenticated_rejected(client, mp_seed):
    r1 = client.get("/api/mp/me/profile").json()
    assert r1["code"] == 40100
    r2 = client.post("/api/mp/verify", json={"code": UNUSED_CODE_A}).json()
    assert r2["code"] == 40100
    r3 = client.get("/api/mp/workbench/kpi").json()
    assert r3["code"] == 40100


def test_mp_customer_scope_forbidden_on_merchant_profile(client, mp_seed, customer_headers):
    resp = client.get("/api/mp/me/profile", headers=customer_headers)
    body = resp.json()
    assert resp.status_code == 200
    assert body["code"] == 40301


def test_mp_cross_tenant_rejected(client, mp_seed, headers_b):
    v = client.post("/api/mp/verify", json={"code": UNUSED_CODE_A},
                    headers=headers_b).json()
    assert v["code"] == 44001, v

    q = client.get("/api/mp/verify/query", params={"code": UNUSED_CODE_A},
                   headers=headers_b).json()
    assert q["code"] == 44001

    log = client.get("/api/mp/verify/log", headers=headers_b).json()
    assert log["code"] == 0
    codes = {x["code"] for x in log["data"]}
    assert UNUSED_CODE_A not in codes and USED_CODE_A not in codes


# ---------- 小程序通知（P1） ----------
def _seed_notice(db_session, tid=1, receiver_id=None, title="P1通知"):
    from app.core.tenant_context import set_tenant
    from app.models.mc_config import McNotice

    set_tenant(tid)
    # commit 入库（客户端请求走 SessionLocal 新连接需已提交数据）；
    # 跨用例残留由 mp_seed 的 _truncate(mc_notice) 在每用例前清除
    db_session.add(McNotice(tenant_id=tid, receiver_type="STAFF", receiver_id=receiver_id,
                            type="SYSTEM", title=title, content="内容", link="", is_read=0))
    db_session.commit()


def test_mp_notice_list_and_read_all(client, mp_seed, headers_a, db_session):
    """通知列表含未读数；read-all 后全已读。"""
    _seed_notice(db_session, receiver_id=10)   # 定向给员工 10
    _seed_notice(db_session, receiver_id=None)  # 全员广播
    data = assert_ok(client.get("/api/mp/notice", headers=headers_a))["data"]
    assert data["total"] == 2 and data["unread"] == 2
    r = assert_ok(client.post("/api/mp/notice/read-all", headers=headers_a))
    assert r["data"]["updated"] == 2
    data2 = assert_ok(client.get("/api/mp/notice", headers=headers_a))["data"]
    assert data2["unread"] == 0


def test_mp_notice_other_staff_not_visible(client, mp_seed, headers_a, headers_b, db_session):
    """定向给 A 租户员工 10 的通知，B 租户员工（shopB）看不到。"""
    _seed_notice(db_session, receiver_id=10, title="A定向")
    data = assert_ok(client.get("/api/mp/notice", headers=headers_b))["data"]
    assert data["total"] == 0
    data_a = assert_ok(client.get("/api/mp/notice", headers=headers_a))["data"]
    assert data_a["total"] == 1
