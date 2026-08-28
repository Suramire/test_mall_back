"""订单超时关单任务的跨租户真实持久化回归。"""
from datetime import datetime, timedelta

from app.core.tenant_context import reset, set_tenant
from app.models.gd_goods import GdSkuStock
from app.models.od_order import OdOrder, OdOrderItem
from app.tasks.order_timeout import close_expired_orders


def _order(session, tenant_id: int, order_id: int, sku_id: int, deadline: datetime) -> None:
    session.add(OdOrder(
        id=order_id, tenant_id=tenant_id, order_no=f"QA-TIMEOUT-{order_id}",
        channel="NORMAL", member_id=order_id, member_no=f"M{order_id}",
        status="PENDING_PAY", delivery_type="EXPRESS", goods_amount="10.00",
        pay_amount="10.00", pay_deadline=deadline,
    ))
    session.add(OdOrderItem(
        tenant_id=tenant_id, order_id=order_id, goods_id=order_id,
        sku_id=sku_id, channel="NORMAL", goods_name="超时测试商品",
        goods_type="NORMAL", price="10.00", quantity=2,
        subtotal_amount="20.00",
    ))


def test_close_expired_orders_releases_locks_cross_tenant_and_is_idempotent(engine):
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    now = datetime(2026, 8, 26, 12, 0, 0)
    try:
        # 库存分别属于两个租户，锁定 2 件，可用库存 8 件。
        for tid, sku, oid in ((1001, 901, 1901), (2002, 902, 2902)):
            set_tenant(tid)
            session.add(GdSkuStock(
                tenant_id=tid, goods_id=oid, sku_id=sku, channel="NORMAL",
                total_stock=10, locked_stock=2, available_stock=8,
            ))
            _order(session, tid, oid, sku, now - timedelta(minutes=1))
            session.flush()
        # 同库存在一笔未到期订单，不应被处理。
        set_tenant(1001)
        session.add(GdSkuStock(
            tenant_id=1001, goods_id=1902, sku_id=903, channel="NORMAL",
            total_stock=10, locked_stock=1, available_stock=9,
        ))
        _order(session, 1001, 1902, 903, now + timedelta(minutes=1))
        session.commit()
        reset()

        assert close_expired_orders(session=session, now=now) == 2
        reset()
        for tid, sku, oid in ((1001, 901, 1901), (2002, 902, 2902)):
            set_tenant(tid)
            order = session.get(OdOrder, oid)
            stock = session.query(GdSkuStock).filter_by(
                tenant_id=tid, sku_id=sku, channel="NORMAL"
            ).one()
            assert order.status == "CLOSED"
            assert order.expired_at == now
            assert order.cancelled_at == now
            assert order.cancel_reason == "PAY_TIMEOUT"
            assert stock.locked_stock == 0 and stock.available_stock == 10

        set_tenant(1001)
        untouched = session.get(OdOrder, 1902)
        stock = session.query(GdSkuStock).filter_by(
            tenant_id=1001, sku_id=903, channel="NORMAL"
        ).one()
        assert untouched.status == "PENDING_PAY"
        assert stock.locked_stock == 1 and stock.available_stock == 9

        # 重入不重复计数，也不重复释放库存。
        assert close_expired_orders(session=session, now=now) == 0
        assert stock.locked_stock == 1 and stock.available_stock == 9
    finally:
        reset()
        session.rollback()
        session.close()
