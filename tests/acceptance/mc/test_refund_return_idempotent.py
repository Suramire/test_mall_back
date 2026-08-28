"""缺陷 A 回归：refund_return 库存返还幂等（ref_type 参数化）。

背景：_already_applied 原先硬编码 ref_type="ORDER"，而 refund_return 写日志时
用 ref_type="REFUND"——幂等检查查不到自己的记录，同一退款单若被重复触发
（如未来重新审批流程）会双返库。修复后 refund_return 传 ref_type="REFUND"，
与 _write_log 写入一致。
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from app.core.errors import BizCode
from app.core.security import SCOPE_CUSTOMER, SCOPE_MERCHANT, create_access_token
from app.core.tenant_context import reset, set_tenant
from app.models.gd_goods import GdSkuStock, GdStockLog
from app.models.mb_member import MbMember
from app.models.od_order import OdOrder, OdOrderItem, OdRefund
from app.services import inventory
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


def _seed_order(db_session, tenant_id: int, member_id: int, oid: int, sku_id: int) -> int:
    """造一笔已支付订单 + 库存行（sold=1 以便退款返还），返回 refund_id。"""
    set_tenant(tenant_id)
    try:
        db_session.add(MbMember(id=member_id, tenant_id=tenant_id, member_no=f"QAA{member_id}"))
        db_session.add(OdOrder(id=oid, tenant_id=tenant_id, order_no=f"QA-A-{oid}", channel="NORMAL",
                               member_id=member_id, status="PAID", delivery_type="EXPRESS",
                               goods_amount=Decimal("9.90"), pay_amount=Decimal("9.90")))
        db_session.add(OdOrderItem(tenant_id=tenant_id, order_id=oid, goods_id=oid, sku_id=sku_id,
                                   channel="NORMAL", goods_name="QA幂等商品", goods_type="PHYSICAL",
                                   price=Decimal("9.90"), quantity=1, subtotal_amount=Decimal("9.90")))
        db_session.add(GdSkuStock(tenant_id=tenant_id, goods_id=oid, sku_id=sku_id, channel="NORMAL",
                                  total_stock=10, available_stock=9, sold_stock=1))
        db_session.commit()
    finally:
        reset()
    return oid


def test_refund_return_idempotent_double_audit(client, db_session):
    """同一退款单重复走退款通过：库存返还只发生一次（ref_type=REFUND 幂等生效）。"""
    base = 73_000_000 + int(uuid.uuid4().int % 10_000_000)
    tenant, member, oid, sku = 1001, base, base + 100, base + 200
    _seed_order(db_session, tenant, member, oid, sku)

    # 用户申请退款
    body = assert_biz_code(client.post("/api/c/refund", headers=_cust(member, tenant),
                                       json={"orderId": oid, "reasonCode": "OTHER", "reasonDesc": "QA"}), BizCode.OK)["data"]
    refund_id = body["id"]
    # 商家审核通过（第一次：正常返还）
    assert_biz_code(client.post(f"/api/mc/refund/{refund_id}/audit", headers=_merch(tenant),
                                json={"approved": True}), BizCode.OK)

    set_tenant(tenant)
    try:
        r = db_session.query(OdRefund).filter_by(id=refund_id, tenant_id=tenant).one()
        assert r.status == "APPROVED"
        stock = db_session.query(GdSkuStock).filter_by(sku_id=sku, tenant_id=tenant).one()
        assert stock.sold_stock == 0, f"退款通过后 sold_stock 应归 0，实际 {stock.sold}"
        assert stock.available_stock == 10, f"退款通过后 available 应回补到 10，实际 {stock.available_stock}"
        # 直接模拟重复触发（如未来重新审批路径）：同一订单再次 refund_return
        set_tenant(tenant)
        inventory.refund_return(
            db_session,
            [{"skuId": sku, "channel": "NORMAL", "qty": 1}],
            f"QA-A-{oid}",
        )
        db_session.flush()
        stock2 = db_session.query(GdSkuStock).filter_by(sku_id=sku, tenant_id=tenant).one()
        assert stock2.sold_stock == 0, "重复退款返还不得再次扣 sold_stock（幂等失效=双返库）"
        logs = db_session.query(GdStockLog).filter_by(
            tenant_id=tenant, sku_id=sku, change_type="REFUND_RETURN").all()
        assert len(logs) == 1, f"REFUND_RETURN 流水应仅 1 条，实际 {len(logs)}"
    finally:
        reset()
