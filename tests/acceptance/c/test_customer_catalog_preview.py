"""用户端分类、商品浏览与订单预览的真实数据验收。"""
from __future__ import annotations

from decimal import Decimal
import uuid

from app.core.errors import BizCode
from app.core.security import SCOPE_CUSTOMER, create_access_token
from app.core.tenant_context import reset, set_tenant
from app.models.gd_goods import GdCategory, GdGoods, GdSku, GdSkuStock
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


def _headers(member_id: int, tenant_id: int) -> dict[str, str]:
    return {"Authorization": "Bearer " + create_access_token(str(member_id), SCOPE_CUSTOMER, tenant_id=tenant_id)}


def _seed(db) -> dict[str, int]:
    # 会话级 SQLite 数据库被多用例共享，避免固定主键与其他验收夹具冲突。
    member_a = 30_000_000 + int(uuid.uuid4().int % 10_000_000)
    member_b = member_a + 10_000_000
    set_tenant(1001)
    try:
        db.add(MbMember(id=member_a, tenant_id=1001, member_no=f"QACAT{member_a}", nickname="QA会员A"))
        category = GdCategory(tenant_id=1001, channel="NORMAL", name="QA分类", sort=10)
        hidden_category = GdCategory(tenant_id=1001, channel="POINTS", name="QA积分分类", sort=9)
        db.add_all([category, hidden_category]); db.flush()
        visible = GdGoods(tenant_id=1001, name="QA可售商品", type="PHYSICAL", channel="NORMAL", status="ON_SALE", normal_on_sale=1, normal_category_id=category.id)
        off_sale = GdGoods(tenant_id=1001, name="QA下架商品", type="PHYSICAL", channel="NORMAL", status="OFF_SALE", normal_on_sale=0, normal_category_id=category.id)
        db.add_all([visible, off_sale]); db.flush()
        sku = GdSku(tenant_id=1001, goods_id=visible.id, sku_code="QA-CAT-SKU", price=Decimal("12.50"))
        db.add(sku); db.flush()
        db.add(GdSkuStock(tenant_id=1001, goods_id=visible.id, sku_id=sku.id, channel="NORMAL", total_stock=3, available_stock=3))
        off_sku = GdSku(tenant_id=1001, goods_id=off_sale.id, sku_code="QA-OFF-SKU", price=Decimal("9.90"))
        db.add(off_sku)
        db.commit()
        category_id, visible_id, sku_id, off_sku_id = category.id, visible.id, sku.id, off_sku.id
    finally:
        reset()
    set_tenant(2002)
    try:
        db.add(MbMember(id=member_b, tenant_id=2002, member_no=f"QACAT{member_b}", nickname="QA会员B"))
        other = GdGoods(tenant_id=2002, name="QA租户B商品", type="PHYSICAL", channel="NORMAL", status="ON_SALE", normal_on_sale=1)
        db.add(other); db.flush()
        other_sku = GdSku(tenant_id=2002, goods_id=other.id, sku_code="QA-B-SKU", price=Decimal("1.00"))
        db.add(other_sku); db.flush()
        db.add(GdSkuStock(tenant_id=2002, goods_id=other.id, sku_id=other_sku.id, channel="NORMAL", total_stock=9, available_stock=9))
        db.commit()
        other_sku_id = other_sku.id
    finally:
        reset()
    return {"member_a":member_a,"member_b":member_b,"category": category_id, "visible": visible_id, "sku": sku_id, "off_sku": off_sku_id, "other_sku": other_sku_id}


def test_customer_category_goods_and_order_preview(client, db_session):
    ids = _seed(db_session)
    h1, h2 = _headers(ids['member_a'], 1001), _headers(ids['member_b'], 2002)

    categories = assert_biz_code(client.get("/api/c/category", headers=h1), BizCode.OK)["data"]
    assert [x["id"] for x in categories] == [ids["category"]]
    goods = assert_biz_code(client.get("/api/c/goods", headers=h1, params={"keyword": "可售", "categoryId": ids["category"]}), BizCode.OK)["data"]
    assert goods["total"] == 1 and goods["list"][0]["id"] == ids["visible"]
    assert assert_biz_code(client.get("/api/c/goods", headers=h1, params={"keyword": "下架"}), BizCode.OK)["data"]["total"] == 0
    assert assert_biz_code(client.get("/api/c/goods", headers=h2, params={"keyword": "QA"}), BizCode.OK)["data"]["total"] == 1

    preview = assert_biz_code(client.post("/api/c/order/preview", headers=h1, json={"items": [{"skuId": ids["sku"], "quantity": 2, "price": "0.01", "subtotalAmount": "0.02"}], "payAmount": "0.02"}), BizCode.OK)["data"]
    assert preview["goodsAmount"] == "25.00" and preview["payAmount"] == "25.00"
    assert preview["items"][0]["price"] == "12.50" and preview["items"][0]["subtotalAmount"] == "25.00"

    assert_biz_code(client.post("/api/c/order/preview", headers=h1, json={"items": []}), BizCode.PARAM_ERROR)
    assert_biz_code(client.post("/api/c/order/preview", headers=h1, json={"items": [{"skuId": ids["sku"], "quantity": 0}]}), BizCode.PARAM_ERROR)
    assert_biz_code(client.post("/api/c/order/preview", headers=h1, json={"items": [{"skuId": ids["sku"], "quantity": "not-a-number"}]}), BizCode.PARAM_ERROR)
    assert_biz_code(client.post("/api/c/order/preview", headers=h1, json={"items": [{"skuId": 999999, "quantity": 1}]}), BizCode.ORDER_SKU_INVALID)
    assert_biz_code(client.post("/api/c/order/preview", headers=h1, json={"items": [{"skuId": ids["off_sku"], "quantity": 1}]}), BizCode.ORDER_GOODS_OFF_SALE)
    # 预览也必须以可用库存为边界，且租户 A 不得枚举或预览租户 B SKU。
    assert_biz_code(client.post("/api/c/order/preview", headers=h1, json={"items": [{"skuId": ids["sku"], "quantity": 4}]}), BizCode.STOCK_NOT_ENOUGH)
    assert_biz_code(client.post("/api/c/order/preview", headers=h1, json={"items": [{"skuId": ids["other_sku"], "quantity": 1}]}), BizCode.ORDER_SKU_INVALID)
