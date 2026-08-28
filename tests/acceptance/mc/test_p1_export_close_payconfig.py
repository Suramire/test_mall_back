"""商家端 P1 契约验收：订单/会员/支付导出、支付配置、订单关闭、消息配置校验。

复用 mc conftest 的 seed_tenants/created/token_a/headers_a。
导出接口为 POST + StreamingResponse（CSV），不走 assert_ok 信封。
"""
from tests.acceptance.mc.conftest import TENANT_A
from tests.conftest import assert_biz_code, assert_ok


def _make_pending_pay_order(db_session, tid=1, oid=9001, mid=201):
    """造一笔 PENDING_PAY 订单（seed 全是 PAID），返回订单 id。"""
    from datetime import datetime

    from app.core.tenant_context import set_tenant
    from app.models.od_order import OdOrder, OdOrderItem

    set_tenant(tid)
    from app.services.inventory import lock_stock

    lock_stock(db_session, [{"sku_id": 1, "channel": "NORMAL", "qty": 1}],
               ref_id=str(oid))
    now = datetime(2026, 8, 27, 10, 0, 0)  # noqa: DTZ001
    o = OdOrder(tenant_id=tid, id=oid, order_no=f"ORD-P1-{oid}",
                channel="NORMAL", member_id=mid, member_no=f"M{mid}",
                status="PENDING_PAY", delivery_type="EXPRESS",
                goods_amount="12.50", pay_amount="12.50",
                created_at=now)
    db_session.add(o)
    db_session.add(OdOrderItem(tenant_id=tid, order_id=oid, goods_id=101, sku_id=1,
                               channel="NORMAL", goods_name="A租户商品",
                               goods_type="NORMAL", spec_text="", price="12.50",
                               quantity=1, subtotal_amount="12.50"))
    db_session.commit()
    return oid


def test_mc_order_close_releases_lock(client, created, headers_a, db_session):
    """PENDING_PAY 订单可关闭；非待付款拒绝；不存在的订单报错。"""
    oid = _make_pending_pay_order(db_session)
    r = assert_ok(client.post(f"/api/mc/order/{oid}/close", headers=headers_a))
    assert r["data"]["status"] == "CLOSED"
    # 非待付款（PAID 1002）应拒绝
    assert_biz_code(client.post("/api/mc/order/1002/close", headers=headers_a), 43006)
    # 不存在的订单 → 非 0 code
    assert client.post("/api/mc/order/99999/close", headers=headers_a).json()["code"] != 0


def test_mc_order_export_csv(client, created, headers_a):
    """订单导出返回 CSV（BOM + 表头 + 至少一行）。"""
    r = client.post("/api/mc/order/export", headers=headers_a)
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    body = r.content.decode("utf-8-sig")
    assert "订单号" in body and "实付" in body
    assert "ORD" in body


def test_mc_member_export_csv(client, created, headers_a):
    r = client.post("/api/mc/member/export", headers=headers_a)
    assert r.status_code == 200
    body = r.content.decode("utf-8-sig")
    assert "会员号" in body and "MA0001" in body


def test_mc_payment_list_and_export(client, created, headers_a, db_session):
    """支付流水分页 + 导出。seed 无支付记录，先造一笔再验证。"""
    from datetime import datetime

    from app.core.tenant_context import set_tenant
    from app.models.od_order import OdPayment

    set_tenant(TENANT_A)
    db_session.add(OdPayment(tenant_id=TENANT_A, order_id=1001,
                             out_trade_no="OUT-P1-001", transaction_id="TX-P1-001",
                             pay_method="WECHAT", channel="NORMAL", amount="12.50",
                             points=0, status="SUCCESS",
                             paid_at=datetime(2026, 8, 27, 10, 0, 0))),  # noqa: DTZ001
    db_session.commit()
    data = assert_ok(client.get("/api/mc/payment", headers=headers_a))["data"]
    assert data["total"] >= 1 and data["list"]
    first = data["list"][0]
    for k in ("orderId", "transactionId", "payMethod", "amount", "status"):
        assert k in first
    r = client.post("/api/mc/payment/export", headers=headers_a)
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    body = r.content.decode("utf-8-sig")
    assert "订单号" in body and "微信单号" in body


def test_mc_pay_config_get_put(client, created, headers_a):
    """支付配置：首次 GET 自动建默认行；PUT DIRECT 生效；PARTNER 必填校验。"""
    data = assert_ok(client.get("/api/mc/pay-config", headers=headers_a))["data"]
    assert data["payMode"] in ("DIRECT", "PARTNER") and "enabled" in data
    # 密钥脱敏：不返回明文
    assert data["wxMchId"] is None or str(data["wxMchId"]).startswith("****")
    # PUT DIRECT + 密钥
    r = assert_ok(client.put("/api/mc/pay-config", headers=headers_a, json={
        "payMode": "DIRECT", "wxMchId": "1900000109", "wxApiKey": "secret-key-123456",
        "notifyUrl": "https://example.com/notify",
    }))
    assert r["data"]["payMode"] == "DIRECT"
    # 读回仍是脱敏
    data2 = assert_ok(client.get("/api/mc/pay-config", headers=headers_a))["data"]
    assert data2["wxMchId"] == "****0109"
    assert data2["notifyUrl"] == "https://example.com/notify"
    # PARTNER 缺 spMchId → 40001
    assert_biz_code(client.put("/api/mc/pay-config", headers=headers_a,
                               json={"payMode": "PARTNER"}), 40001)


def test_mc_pay_config_partner_roundtrip(client, created, headers_a):
    r = assert_ok(client.put("/api/mc/pay-config", headers=headers_a, json={
        "payMode": "PARTNER", "spMchId": "sp-001", "subMchId": "sub-002",
        "subAppid": "wxappid123", "notifyUrl": "https://example.com/pay",
    }))
    assert r["data"]["payMode"] == "PARTNER"
    data = assert_ok(client.get("/api/mc/pay-config", headers=headers_a))["data"]
    assert data["payMode"] == "PARTNER"
    assert str(data["spMchId"]).startswith("****")


def test_mc_msg_config_channels_validation(client, created, headers_a):
    """消息配置：非法 channels 拒绝；合法放行。"""
    cfg_id = assert_ok(client.get("/api/mc/msg-config", headers=headers_a))["data"][0]["id"]
    assert_biz_code(client.put(f"/api/mc/msg-config/{cfg_id}", headers=headers_a,
                               json={"channels": ["EMAIL"]}), 40001)
    assert_biz_code(client.put(f"/api/mc/msg-config/{cfg_id}", headers=headers_a,
                               json={"enabled": 2}), 40001)
    assert_ok(client.put(f"/api/mc/msg-config/{cfg_id}", headers=headers_a,
                         json={"channels": ["WX_SUBSCRIBE", "INTERNAL"], "enabled": 1}))
