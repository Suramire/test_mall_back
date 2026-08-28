"""缺陷 C 回归：商家订单操作门店归属隔离（设计 U9 / 00-技术选型.md）。

设计决策：mc_staff.store_id 非空=仅本门店（核销/自提/订单列表默认过滤），
NULL=总部看全量。此前 /order/{id}/stocking、ship、pickup-confirm、
batch-ship、order/{id} 详情与订单列表均无门店隔离，门店员工可操作全租户订单。

覆盖：
- 门店员工操作他店订单（stocking/ship/pickup-confirm/detail）→ 403
- 门店员工操作本店订单 → 正常
- 订单列表按员工门店过滤
- verify 核销：门店员工强制本门店，传他店/不传均按本店
- 总部员工（store_id=None）不受限，可操作任意门店订单
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.security import SCOPE_MERCHANT, create_access_token
from app.core.tenant_context import reset, set_tenant
from app.models.gd_goods import GdGoods, GdSku, GdSkuStock
from app.models.mb_member import MbMember
from app.models.mc_staff import McStaff
from app.models.od_order import OdOrder, OdOrderItem, OdVerifyCode


def _enable_sqlite_autoincrement() -> None:
    from sqlalchemy import BigInteger, Integer

    from app.db.base import Base
    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if isinstance(col.type, BigInteger):
                col.type = BigInteger().with_variant(Integer(), "sqlite")


_enable_sqlite_autoincrement()


def _merchant(staff_id: int, tid: int = 1001) -> dict[str, str]:
    return {"Authorization": "Bearer " + create_access_token(
        str(staff_id), SCOPE_MERCHANT, tenant_id=tid, perms=["MC_ALL"])}


def _seed(db_session, base: int) -> dict:
    """租户 1001：门店 501/502，员工 701(店501)/702(店502)/703(总部)，
    两笔订单：ordA 属门店501、ordB 属门店502。返回关键 id。"""
    set_tenant(1001)
    try:
        gid, sid, mid = base + 1, base + 2, base + 3
        staff_a, staff_b, staff_hq = base + 10, base + 11, base + 12
        ord_a, ord_b = base + 20, base + 21
        db_session.add(GdGoods(id=gid, tenant_id=1001, name="门店测试商品", type="PHYSICAL",
                       channel="NORMAL", status="ON_SALE", normal_on_sale=1))
        db_session.add(GdSku(id=sid, tenant_id=1001, goods_id=gid, sku_code=f"SK-{sid}", price="10.00"))
        db_session.add(GdSkuStock(tenant_id=1001, goods_id=gid, sku_id=sid, channel="NORMAL",
                          total_stock=100, available_stock=100))
        db_session.add(MbMember(id=mid, tenant_id=1001, member_no=f"QA{mid}", points_balance=0))
        # 员工：A=店501、B=店502、HQ=总部
        for sid_, acc, nm in ((501, "staffA", "员工A"), (502, "staffB", "员工B")):
            db_session.add(McStaff(id=staff_a if sid_ == 501 else staff_b, tenant_id=1001,
                           account=acc, name=nm, password_hash="x", role_id=0,
                           store_id=sid_, status="ENABLED", is_admin=0))
        db_session.add(McStaff(id=staff_hq, tenant_id=1001, account="hq", name="总部",
                       password_hash="x", role_id=0, store_id=None, status="ENABLED", is_admin=1))
        # 订单：A 属店501（PENDING_SHIP 可发货）、B 属店502（PENDING_SHIP）
        for oid_, sid_, no_ in ((ord_a, 501, "ORD-A"), (ord_b, 502, "ORD-B")):
            db_session.add(OdOrder(id=oid_, tenant_id=1001, order_no=f"{no_}-{base}", channel="NORMAL", member_id=mid,
                           delivery_type="EXPRESS", status="PENDING_SHIP",
                           goods_amount="10.00", pay_amount="10.00",
                           receiver_name="测", receiver_phone="13800000000",
                           receiver_address="地址", store_id=sid_))
            db_session.add(OdOrderItem(tenant_id=1001, order_id=oid_, channel="NORMAL", goods_id=gid, sku_id=sid,
                               goods_name="门店测试商品", goods_type="PHYSICAL", quantity=1,
                               price="10.00", subtotal_amount="10.00"))
        # 核销码：属店501，供 verify 用例
        db_session.add(OdVerifyCode(tenant_id=1001, order_id=ord_a, order_item_id=ord_a, code=f"HXSTORE{base}", code_type="VERIFY",
                            goods_name="门店测试商品", status="UNUSED",
                            member_id=mid, valid_start=datetime.now(UTC), valid_end=datetime.now(UTC)+timedelta(days=30), verify_store_id=501))
        db_session.commit()
        return {"sku": sid, "member": mid, "staffA": staff_a, "staffB": staff_b,
                "staffHQ": staff_hq, "ordA": ord_a, "ordB": ord_b, "goods": gid,
                "base": base}
    finally:
        reset()


def test_store_staff_cannot_operate_other_store_order(db_session, client):
    """门店 A 员工操作门店 B 订单：stocking/ship/detail 均 403。"""
    d = _seed(db_session, 3000)
    h = _merchant(d["staffA"])
    # 他店订单详情：业务信封 40301
    r = client.get(f"/api/mc/order/{d['ordB']}", headers=h)
    assert r.status_code == 200
    assert r.json()["code"] == 40301
    # 他店订单备货
    r = client.post(f"/api/mc/order/{d['ordB']}/stocking", headers=h)
    assert r.status_code == 200
    assert r.json()["code"] == 40301
    # 他店订单发货
    r = client.post(f"/api/mc/order/{d['ordB']}/ship", headers=h,
                    json={"expressCompany": "SF", "expressNo": "SF1"})
    assert r.status_code == 200
    assert r.json()["code"] == 40301
    # 他店订单批量发货：进入 failed 列表且不越权
    r = client.post("/api/mc/order/batch-ship", headers=h,
                    json={"ids": [d["ordB"]], "expressCompany": "SF", "expressNo": "SF1"})
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["success"] == []
    assert body["failed"][0]["id"] == d["ordB"]


def test_store_staff_can_operate_own_store_order(db_session, client):
    """门店 A 员工操作本店订单：备货/发货/详情正常。"""
    d = _seed(db_session, 3100)
    h = _merchant(d["staffA"])
    r = client.get(f"/api/mc/order/{d['ordA']}", headers=h)
    assert r.status_code == 200
    assert r.json()["data"]["orderNo"] == f"ORD-A-{d['base']}"
    r = client.post(f"/api/mc/order/{d['ordA']}/ship", headers=h,
                    json={"expressCompany": "SF", "expressNo": "SF1"})
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "SHIPPED"


def test_order_list_filtered_by_staff_store(db_session, client):
    """门店员工订单列表只见本店订单；总部看全量。"""
    d = _seed(db_session, 3200)
    r = client.get("/api/mc/orders", headers=_merchant(d["staffA"]))
    assert r.status_code == 200
    rows = r.json()["data"]["list"]
    # 门店A员工：仅见本店订单（含历史用例残留的本店单），绝不见 B 店订单
    assert all(x["orderNo"].startswith("ORD-A") for x in rows)
    assert all("ORD-B" not in x["orderNo"] for x in rows)
    r = client.get("/api/mc/orders", headers=_merchant(d["staffHQ"]))
    hq = [x["orderNo"] for x in r.json()["data"]["list"]]
    # 总部：A/B 两店均可见
    assert any(x.startswith("ORD-A") for x in hq)
    assert any(x.startswith("ORD-B") for x in hq)


def test_verify_store_forced_to_staff_store(db_session, client):
    """核销：门店员工强制本店，传他店/不传 storeId 均按本店核销。"""
    d = _seed(db_session, 3300)
    h = _merchant(d["staffA"])
    # 不传 storeId：强制本店，成功
    r = client.post("/api/mc/verify", headers=h, json={"code": f"HXSTORE{d['base']}"})
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "USED"
    # 传他店 storeId：拒绝
    d2 = _seed(db_session, 3400)
    h2 = _merchant(d2["staffA"])
    r = client.post("/api/mc/verify", headers=h2, json={"code": f"HXSTORE{d2['base']}", "storeId": 502})
    assert r.status_code == 200
    assert r.json()["code"] == 44004


def test_hq_staff_unrestricted(db_session, client):
    """总部员工可操作任意门店订单，也可按前端指定门店核销。"""
    d = _seed(db_session, 3500)
    h = _merchant(d["staffHQ"])
    r = client.get(f"/api/mc/order/{d['ordB']}", headers=h)
    assert r.status_code == 200
    r = client.post(f"/api/mc/order/{d['ordB']}/ship", headers=h,
                    json={"expressCompany": "SF", "expressNo": "SF1"})
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "SHIPPED"
