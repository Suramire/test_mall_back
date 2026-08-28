"""T-032 商品/SKU/库存（三段式锁）`/api/mc/goods` 验收用例。

依据：docs/architecture/02-数据库设计.md §3（gd_goods/gd_sku/gd_sku_stock）、
03-API设计.md §3.3（错误码 41003/42004/42007/42008）、04 §2.2 下单支付时序图。

覆盖：
  1. 商品 CRUD + SKU/双渠道库存真实落库（裸 SQL 断言，不走 mock）
  2. 双渠道独立上下架（R-CH-04）+ 信息不全 42004
  3. 库存三模式调整（INCREASE/DECREASE/SET）+ 42007/42008 + 流水
  4. 三段式锁 service 级：下单锁 → 支付扣 / 超时释放、CAS 防超卖、幂等
  5. 商品配额 41003、perms 守卫 40301、跨租户隔离
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.errors import BizCode
from app.core.exceptions import BizError

from .conftest import TENANT_A, assert_code, assert_envelope

MC = "/api/mc"


def db_rows(engine, sql: str, **params):
    """裸 SQL 查库：绕过 ORM 钩子与软删，断言的是真实持久化结果。"""
    with engine.connect() as conn:
        return conn.execute(text(sql), params).mappings().all()


def db_one(engine, sql: str, **params):
    rows = db_rows(engine, sql, **params)
    assert rows, f"未查到数据：{sql} {params}"
    return rows[0]


def _mk_goods_payload(name: str, channel: str = "BOTH", with_stock: bool = True) -> dict:
    """一个字段齐备的 TICKET 核销券商品（双渠道 + 双 SKU 库存）。"""
    stocks = (
        [{"channel": "NORMAL", "totalStock": 100, "warnStock": 10},
         {"channel": "POINTS", "totalStock": 50, "warnStock": 5}]
        if with_stock else []
    )
    return {
        "name": name,
        "type": "TICKET",
        "channel": channel,
        "mainImage": "https://cdn.example.com/main.png",
        "images": ["https://cdn.example.com/1.png"],
        "detail": "<p>详情</p>",
        "skus": [
            {
                "specJson": {"版本": "标准"},
                "specText": "标准",
                "price": "199.00",
                "originalPrice": "299.00",
                "priceMode": "MIXED",
                "points": 500,
                "cash": "9.90",
                "stocks": stocks,
            }
        ],
        "ticketConfig": {
            "validType": "DAYS_AFTER_PAY",
            "validDays": 30,
            "verifyDesc": "到店出示核销码",
            "expireRefundPolicy": "FULL_CASH",
        },
        "pointsLimitPerUser": 1,
    }


# ===========================================================================
# 1. 商品 CRUD + 持久化
# ===========================================================================
class TestGoodsCrud:
    def test_create_full_goods_persists(self, client, seed_tenants, headers_a, engine):
        """新增（SKU 数组 + 双渠道库存 + 核销券配置）→ 三张表真实落库。"""
        resp = client.post(f"{MC}/goods", headers=headers_a,
                           json=_mk_goods_payload("轻钛镜架"))
        body = assert_code(resp, BizCode.OK)
        gid = body["data"]["id"]
        assert gid and body["data"]["status"] == "DRAFT"

        g = db_one(engine, "SELECT * FROM gd_goods WHERE id=:id", id=gid)
        assert g["tenant_id"] == TENANT_A
        assert g["type"] == "TICKET" and g["channel"] == "BOTH"
        assert g["valid_type"] == "DAYS_AFTER_PAY" and g["valid_days"] == 30

        skus = db_rows(engine, "SELECT * FROM gd_sku WHERE goods_id=:gid", gid=gid)
        assert len(skus) == 1
        # sku_code 规则：SKU + 商品ID + 3位（DDL 注释口径）
        assert skus[0]["sku_code"].startswith("SKU"), skus[0]["sku_code"]
        assert str(gid) in skus[0]["sku_code"]
        assert skus[0]["price_mode"] == "MIXED" and skus[0]["points"] == 500
        assert float(skus[0]["price"]) == 199.0  # 金额落库（MySQL 侧 DECIMAL(10,2)）

        stocks = db_rows(
            engine,
            "SELECT channel,total_stock,available_stock FROM gd_sku_stock "
            "WHERE goods_id=:gid ORDER BY channel", gid=gid)
        assert {r["channel"]: r["total_stock"] for r in stocks} == \
            {"NORMAL": 100, "POINTS": 50}
        assert all(r["available_stock"] == r["total_stock"] for r in stocks)

    def test_list_stock_column_is_sum_of_channels(self, client, seed_tenants,
                                                  headers_a, engine):
        """列表库存列 = 两渠道 available 之和（03-API设计.md §3.3）。"""
        create = client.post(f"{MC}/goods", headers=headers_a,
                             json=_mk_goods_payload("双渠道库存商品"))
        gid = assert_code(create, BizCode.OK)["data"]["id"]
        body = assert_code(client.get(f"{MC}/goods", headers=headers_a,
                                      params={"keyword": "双渠道库存商品"}),
                           BizCode.OK)
        row = next(x for x in body["data"]["list"] if x["id"] == gid)
        assert row["stock"] == 150, f"库存列应为两渠道总和 150，实际 {row['stock']}"

    def test_list_filters_and_sold_out_derived(self, client, seed_tenants,
                                               headers_a, engine):
        """status 过滤 + SOLD_OUT 派生状态（不落库，列表计算）。"""
        # 有 SKU 无库存的商品上架 → ON_SALE 但派生 SOLD_OUT
        payload = _mk_goods_payload("零库存商品", with_stock=False)
        gid = assert_code(client.post(f"{MC}/goods", headers=headers_a, json=payload),
                          BizCode.OK)["data"]["id"]
        assert_code(client.post(f"{MC}/goods/{gid}/shelf", headers=headers_a,
                                json={"channel": "NORMAL", "onSale": True}),
                    BizCode.OK)
        body = assert_code(client.get(f"{MC}/goods", headers=headers_a,
                                      params={"status": "ON_SALE"}), BizCode.OK)
        row = next(x for x in body["data"]["list"] if x["id"] == gid)
        assert row["status"] == "ON_SALE" and row["derivedStatus"] == "SOLD_OUT"
        # 落库 status 仍是 ON_SALE（SOLD_OUT 为派生态不落库）
        assert db_one(engine, "SELECT status FROM gd_goods WHERE id=:id",
                      id=gid)["status"] == "ON_SALE"

    def test_detail_contains_skus_stocks_ticket(self, client, seed_tenants, headers_a):
        """详情含 skus[] + 双渠道库存 + ticketConfig。"""
        gid = assert_code(client.post(f"{MC}/goods", headers=headers_a,
                                      json=_mk_goods_payload("详情商品")),
                          BizCode.OK)["data"]["id"]
        data = assert_code(client.get(f"{MC}/goods/{gid}", headers=headers_a),
                           BizCode.OK)["data"]
        assert data["id"] == gid and data["name"] == "详情商品"
        assert len(data["skus"]) == 1
        sku = data["skus"][0]
        assert sku["price"] == "199.00" and sku["points"] == 500
        ch = {s["channel"]: s for s in sku["stocks"]}
        assert ch["NORMAL"]["totalStock"] == 100 and ch["POINTS"]["totalStock"] == 50
        assert data["ticketConfig"]["validType"] == "DAYS_AFTER_PAY"
        assert data["ticketConfig"]["validDays"] == 30

    def test_update_and_append_sku(self, client, seed_tenants, headers_a, engine):
        """编辑 SPU 字段 + 追加 SKU（无 id 视为新增）。"""
        gid = assert_code(client.post(f"{MC}/goods", headers=headers_a,
                                      json=_mk_goods_payload("待编辑商品")),
                          BizCode.OK)["data"]["id"]
        resp = client.put(f"{MC}/goods/{gid}", headers=headers_a, json={
            "name": "改名后商品",
            "skus": [{"specText": "加大", "price": "259.00", "priceMode": "CASH"}],
        })
        assert_code(resp, BizCode.OK)
        assert db_one(engine, "SELECT name FROM gd_goods WHERE id=:id",
                      id=gid)["name"] == "改名后商品"
        assert len(db_rows(engine, "SELECT id FROM gd_sku WHERE goods_id=:gid "
                           "AND deleted_at IS NULL", gid=gid)) == 2

    def test_delete_is_soft(self, client, seed_tenants, headers_a, engine):
        gid = assert_code(client.post(f"{MC}/goods", headers=headers_a,
                                      json=_mk_goods_payload("待删商品")),
                          BizCode.OK)["data"]["id"]
        assert_code(client.delete(f"{MC}/goods/{gid}", headers=headers_a), BizCode.OK)
        row = db_one(engine, "SELECT deleted_at,status FROM gd_goods WHERE id=:id",
                     id=gid)
        assert row["deleted_at"] is not None and row["status"] == "OFF_SALE"
        # 软删后详情/列表不可见
        assert_code(client.get(f"{MC}/goods/{gid}", headers=headers_a),
                    BizCode.NOT_FOUND)


# ===========================================================================
# 2. 双渠道独立上下架
# ===========================================================================
class TestShelf:
    def test_channel_independent_shelf(self, client, seed_tenants, headers_a, engine):
        """★按渠道独立上下架：NORMAL 上架不影响 POINTS，反之亦然。"""
        gid = assert_code(client.post(f"{MC}/goods", headers=headers_a,
                                      json=_mk_goods_payload("渠道独立商品")),
                          BizCode.OK)["data"]["id"]

        assert_code(client.post(f"{MC}/goods/{gid}/shelf", headers=headers_a,
                                json={"channel": "NORMAL", "onSale": True}),
                    BizCode.OK)
        row = db_one(engine, "SELECT * FROM gd_goods WHERE id=:id", id=gid)
        assert row["normal_on_sale"] == 1 and row["points_on_sale"] == 0
        assert row["status"] == "ON_SALE"

        assert_code(client.post(f"{MC}/goods/{gid}/shelf", headers=headers_a,
                                json={"channel": "POINTS", "onSale": True}),
                    BizCode.OK)
        row = db_one(engine, "SELECT * FROM gd_goods WHERE id=:id", id=gid)
        assert row["normal_on_sale"] == 1 and row["points_on_sale"] == 1

        # 仅下架 NORMAL，POINTS 保持在售
        assert_code(client.post(f"{MC}/goods/{gid}/shelf", headers=headers_a,
                                json={"channel": "NORMAL", "onSale": False}),
                    BizCode.OK)
        row = db_one(engine, "SELECT * FROM gd_goods WHERE id=:id", id=gid)
        assert row["normal_on_sale"] == 0 and row["points_on_sale"] == 1
        assert row["status"] == "ON_SALE"

    def test_shelf_blocked_when_sku_missing(self, client, seed_tenants, headers_a):
        """无 SKU 上架 → 42004 信息不全。"""
        gid = assert_code(client.post(f"{MC}/goods", headers=headers_a,
                                      json={"name": "裸商品", "type": "PHYSICAL",
                                            "channel": "NORMAL"}),
                          BizCode.OK)["data"]["id"]
        body = assert_envelope(client.post(
            f"{MC}/goods/{gid}/shelf", headers=headers_a,
            json={"channel": "NORMAL", "onSale": True}))
        assert body["code"] == BizCode.GOODS_SHELF_INFO_INCOMPLETE, body

    def test_shelf_points_channel_requires_points_price(
            self, client, seed_tenants, headers_a):
        """种子商品 101 仅 CASH SKU，上 POINTS 渠道 → 42004。"""
        body = assert_envelope(client.post(
            f"{MC}/goods/101/shelf", headers=headers_a,
            json={"channel": "POINTS", "onSale": True}))
        assert body["code"] == BizCode.GOODS_SHELF_INFO_INCOMPLETE, body
        # NORMAL 渠道仍可上架（种子已有现金价 SKU）
        assert_code(client.post(f"{MC}/goods/101/shelf", headers=headers_a,
                                json={"channel": "NORMAL", "onSale": True}),
                    BizCode.OK)


# ===========================================================================
# 3. 库存三模式调整
# ===========================================================================
class TestStockAdjust:
    def test_set_increase_decrease_and_log(self, client, seed_tenants,
                                           headers_a, engine):
        """items 批量三模式 + 流水落库（种子 sku=1 NORMAL total=30）。"""
        resp = client.put(f"{MC}/goods/101/stock", headers=headers_a, json={
            "items": [
                {"skuId": 1, "channel": "NORMAL", "changeType": "SET", "value": 50},
            ]})
        body = assert_code(resp, BizCode.OK)
        assert body["data"]["items"][0]["totalStock"] == 50
        assert body["data"]["items"][0]["availableStock"] == 50

        assert_code(client.put(f"{MC}/goods/101/stock", headers=headers_a, json={
            "items": [
                {"skuId": 1, "channel": "NORMAL", "changeType": "INCREASE", "value": 5},
                {"skuId": 1, "channel": "POINTS", "changeType": "INCREASE", "value": 8},
            ]}), BizCode.OK)

        row = db_one(engine, "SELECT total_stock,available_stock FROM gd_sku_stock "
                             "WHERE sku_id=1 AND channel='NORMAL'")
        assert row["total_stock"] == 55 and row["available_stock"] == 55
        p = db_one(engine, "SELECT total_stock FROM gd_sku_stock "
                           "WHERE sku_id=1 AND channel='POINTS'")
        assert p["total_stock"] == 8  # 原无 POINTS 行，自动建行

        assert_code(client.put(f"{MC}/goods/101/stock", headers=headers_a, json={
            "items": [{"skuId": 1, "channel": "NORMAL",
                       "changeType": "DECREASE", "value": 3}]}), BizCode.OK)
        row = db_one(engine, "SELECT total_stock,available_stock FROM gd_sku_stock "
                             "WHERE sku_id=1 AND channel='NORMAL'")
        assert row["total_stock"] == 52

        logs = db_rows(engine, "SELECT change_type FROM gd_stock_log "
                               "WHERE goods_id=101 ORDER BY id")
        assert [x["change_type"] for x in logs] == \
            ["SET", "INCREASE", "INCREASE", "DECREASE"]

        log_body = assert_code(client.get(f"{MC}/goods/101/stock-log",
                                          headers=headers_a), BizCode.OK)
        assert log_body["data"]["total"] == 4

    def test_decrease_beyond_available_42008(self, client, seed_tenants, headers_a):
        body = assert_envelope(client.put(f"{MC}/goods/101/stock", headers=headers_a,
                                          json={"items": [{"skuId": 1,
                                                           "channel": "NORMAL",
                                                           "changeType": "DECREASE",
                                                           "value": 999}]}))
        assert body["code"] == BizCode.STOCK_NOT_ENOUGH, body

    def test_invalid_value_42007(self, client, seed_tenants, headers_a):
        for bad in ({"changeType": "INCREASE", "value": 0},
                    {"changeType": "DECREASE", "value": -3},
                    {"changeType": "SET", "value": "abc"}):
            body = assert_envelope(client.put(
                f"{MC}/goods/101/stock", headers=headers_a,
                json={"items": [{"skuId": 1, "channel": "NORMAL", **bad}]}))
            assert body["code"] == BizCode.STOCK_INVALID_VALUE, (bad, body)

    def test_flat_legacy_payload_still_accepted(self, client, seed_tenants, headers_a):
        """兼容旧前端扁平请求（无 items、value 用 stock 字段）。"""
        body = assert_code(client.put(f"{MC}/goods/101/stock", headers=headers_a,
                                      json={"skuId": 1, "channel": "NORMAL",
                                            "stock": 40}), BizCode.OK)
        assert body["data"]["items"][0]["totalStock"] == 40


# ===========================================================================
# 4. 三段式库存锁（下单锁 → 支付扣 / 超时释放）
# ===========================================================================
class TestThreePhaseLock:
    """service 级验收：T-033 订单链路将直接调用这些接口。"""

    @pytest.fixture
    def svc_session(self, app, seed_tenants):
        """绑定测试 engine 的 SessionLocal + 租户 A 上下文。"""
        from app.core.tenant_context import reset, set_tenant
        from app.db.session import SessionLocal

        set_tenant(TENANT_A)
        s = SessionLocal()
        yield s
        s.close()
        reset()

    def _stock(self, engine, sku_id=1, channel="NORMAL"):
        return db_one(engine,
                      "SELECT total_stock,locked_stock,sold_stock,available_stock "
                      "FROM gd_sku_stock WHERE sku_id=:sku AND channel=:ch",
                      sku=sku_id, ch=channel)

    def test_lock_then_confirm(self, svc_session, engine):
        """下单锁 10 → 支付扣：locked 归还、sold 前进、available 不再回涨。"""
        from app.services import inventory

        inventory.lock_stock(svc_session, [{"skuId": 1, "channel": "NORMAL",
                                            "qty": 10}], "ORDT32-001")
        svc_session.commit()
        st = self._stock(engine)
        assert (st["total_stock"], st["locked_stock"], st["sold_stock"],
                st["available_stock"]) == (30, 10, 0, 20)

        inventory.confirm_lock(svc_session, [{"skuId": 1, "channel": "NORMAL",
                                              "qty": 10}], "ORDT32-001")
        svc_session.commit()
        st = self._stock(engine)
        assert (st["locked_stock"], st["sold_stock"], st["available_stock"]) == \
            (0, 10, 20)
        types = [x["change_type"] for x in db_rows(
            engine, "SELECT change_type FROM gd_stock_log WHERE ref_id=:r ORDER BY id",
            r="ORDT32-001")]
        assert types == ["ORDER_LOCK", "ORDER_PAY"]

    def test_confirm_is_idempotent_per_order(self, svc_session, engine):
        """支付回调重放：同 ref_id 二次 confirm 不得重复扣减。"""
        from app.services import inventory

        inventory.lock_stock(svc_session, [{"skuId": 1, "channel": "NORMAL",
                                            "qty": 6}], "ORDT32-002")
        svc_session.commit()
        inventory.confirm_lock(svc_session, [{"skuId": 1, "channel": "NORMAL",
                                              "qty": 6}], "ORDT32-002")
        svc_session.commit()
        inventory.confirm_lock(svc_session, [{"skuId": 1, "channel": "NORMAL",
                                              "qty": 6}], "ORDT32-002")  # 重放
        svc_session.commit()
        st = self._stock(engine)
        assert st["sold_stock"] == 6 and st["available_stock"] == 24

    def test_lock_then_release_on_timeout(self, svc_session, engine):
        """超时/关单：locked 归还 available，总量守恒。"""
        from app.services import inventory

        inventory.lock_stock(svc_session, [{"skuId": 1, "channel": "NORMAL",
                                            "qty": 7}], "ORDT32-003")
        svc_session.commit()
        inventory.release_lock(svc_session, [{"skuId": 1, "channel": "NORMAL",
                                              "qty": 7}], "ORDT32-003")
        svc_session.commit()
        # 释放幂等（关单任务可能重入）
        inventory.release_lock(svc_session, [{"skuId": 1, "channel": "NORMAL",
                                              "qty": 7}], "ORDT32-003")
        svc_session.commit()
        st = self._stock(engine)
        assert (st["locked_stock"], st["sold_stock"], st["available_stock"]) == \
            (0, 0, 30)

    def test_lock_rejects_overdraft(self, svc_session, engine):
        """CAS 防超卖：预锁超过 available → 42008 且不落任何变更。"""
        from app.services import inventory

        with pytest.raises(BizError) as ei:
            inventory.lock_stock(svc_session, [{"skuId": 1, "channel": "NORMAL",
                                                "qty": 31}], "ORDT32-004")
        svc_session.rollback()
        assert ei.value.code == BizCode.STOCK_NOT_ENOUGH
        st = self._stock(engine)
        assert (st["locked_stock"], st["available_stock"]) == (0, 30)

    def test_refund_return_restocks(self, svc_session, engine):
        """退款通过：sold 回退、available 回涨。"""
        from app.services import inventory

        inventory.lock_stock(svc_session, [{"skuId": 1, "channel": "NORMAL",
                                            "qty": 4}], "ORDT32-005")
        inventory.confirm_lock(svc_session, [{"skuId": 1, "channel": "NORMAL",
                                              "qty": 4}], "ORDT32-005")
        svc_session.commit()
        inventory.refund_return(svc_session, [{"skuId": 1, "channel": "NORMAL",
                                               "qty": 4}], "RFT32-005")
        svc_session.commit()
        st = self._stock(engine)
        assert (st["sold_stock"], st["available_stock"]) == (0, 30)

    def test_set_cannot_go_below_occupied(self, svc_session, engine, client,
                                          headers_a):
        """SET 不得低于 locked+sold：先锁 10，再 SET 5 → 42008。"""
        from app.services import inventory

        inventory.lock_stock(svc_session, [{"skuId": 1, "channel": "NORMAL",
                                            "qty": 10}], "ORDT32-006")
        svc_session.commit()
        body = assert_envelope(client.put(
            f"{MC}/goods/101/stock", headers=headers_a,
            json={"items": [{"skuId": 1, "channel": "NORMAL",
                             "changeType": "SET", "value": 5}]}))
        assert body["code"] == BizCode.STOCK_NOT_ENOUGH, body


# ===========================================================================
# 5. 配额 / 权限 / 跨租户
# ===========================================================================
class TestQuotaAndGuard:
    @pytest.fixture
    def quota_tenant(self, engine):
        """给租户 A 设 goods_limit=1（种子已有 1 件商品 → 再建即超限）。"""
        from sqlalchemy.orm import sessionmaker

        from app.models.pf_tenant import PfTenant

        Session = sessionmaker(bind=engine)
        s = Session()
        try:
            row = s.get(PfTenant, TENANT_A)
            if row is None:
                s.add(PfTenant(id=TENANT_A, tenant_no="M10001", name="租户A",
                               status="NORMAL", goods_limit=1))
            else:  # 防御：残留行改配额，避免主键冲突
                row.status, row.goods_limit = "NORMAL", 1
            s.commit()
            yield
        finally:
            row = s.get(PfTenant, TENANT_A)
            if row is not None:
                s.delete(row)
                s.commit()
            s.close()

    def test_goods_quota_exceeded_41003(self, client, seed_tenants, headers_a,
                                        quota_tenant):
        body = assert_envelope(client.post(
            f"{MC}/goods", headers=headers_a, json=_mk_goods_payload("超额商品")))
        assert body["code"] == BizCode.TENANT_QUOTA_GOODS, body

    def test_perm_guard_without_mc_all(self, client, seed_tenants):
        """perms 为空（且不含 GOODS_*）→ 40301。"""
        from app.core.security import SCOPE_MERCHANT, create_access_token

        token = create_access_token(subject="10", scope=SCOPE_MERCHANT,
                                    tenant_id=TENANT_A, perms=[])
        resp = client.get(f"{MC}/goods",
                          headers={"Authorization": f"Bearer {token}"})
        body = assert_envelope(resp)
        assert body["code"] == BizCode.FORBIDDEN, body

    def test_cross_tenant_stock_adjust_rejected(self, client, seed_tenants,
                                                headers_b, engine):
        """租户 B 不得调整租户 A 的商品库存（sku=1 属 A）。"""
        body = assert_envelope(client.put(
            f"{MC}/goods/101/stock", headers=headers_b,
            json={"items": [{"skuId": 1, "channel": "NORMAL",
                             "changeType": "SET", "value": 0}]}))
        assert body["code"] != BizCode.OK, "越权成功！B 改了 A 的库存"
        row = db_one(engine, "SELECT total_stock FROM gd_sku_stock "
                             "WHERE sku_id=1 AND channel='NORMAL'")
        assert row["total_stock"] == 30, "库存被越权篡改"

    def test_cross_tenant_shelf_rejected(self, client, seed_tenants, headers_b):
        body = assert_envelope(client.post(
            f"{MC}/goods/101/shelf", headers=headers_b,
            json={"channel": "NORMAL", "onSale": False}))
        assert body["code"] != BizCode.OK
