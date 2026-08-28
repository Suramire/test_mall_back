"""订单超时关闭任务。

可由 Celery/cron 调用 ``close_expired_orders``；函数本身不依赖 worker，便于真实 DB 回归。
"""
from datetime import datetime, timezone
from app.db.session import SessionLocal
from app.models.od_order import OdOrder, OdOrderItem
from app.services import inventory
from app.core.tenant_context import reset, set_tenant
from app.db.orm_hooks import SKIP_OPTION
from app.tasks.celery_app import celery_app

PENDING = ("PENDING_PAY", "PENDING_PAYMENT")


@celery_app.task(name="app.tasks.order_timeout.close_expired_orders_task")
def close_expired_orders_task() -> int:
    """Celery/beat 可执行入口；每次调用创建并关闭独立数据库会话。"""
    return close_expired_orders()

def close_expired_orders(*, session=None, now=None) -> int:
    own = session is None
    s = session or SessionLocal()
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        # 定时任务跨租户扫描必须显式跳过默认租户过滤；处理每笔订单时再恢复
        # 该订单租户上下文，使库存服务仍走同一套隔离逻辑。
        rows = s.query(OdOrder).execution_options(**{SKIP_OPTION: True}).filter(
            OdOrder.status.in_(PENDING), OdOrder.pay_deadline.isnot(None), OdOrder.pay_deadline <= now
        ).all()
        candidates = [(o.id, o.tenant_id) for o in rows]
        count = 0
        for order_id, tenant_id in candidates:
            set_tenant(tenant_id)
            try:
                # 提交上一笔后 ORM 对象已过期，按主键在当前租户上下文重新读取。
                order = s.get(OdOrder, order_id)
                if order is None or order.status not in PENDING or not order.pay_deadline or order.pay_deadline > now:
                    continue
                items = s.query(OdOrderItem).filter_by(tenant_id=order.tenant_id, order_id=order.id).all()
                inventory.release_lock(s, [{"skuId": i.sku_id, "channel": i.channel, "qty": i.quantity} for i in items], order.order_no)
                order.status = "CLOSED"
                order.expired_at = now
                order.cancelled_at = now
                order.cancel_reason = "PAY_TIMEOUT"
                # 每笔订单在其租户上下文内提交，避免跨租户批处理时 ORM
                # before_flush 将前一租户的库存日志误判为越权写入。
                s.commit()
                count += 1
            finally:
                reset()
        return count
    except Exception:
        s.rollback()
        raise
    finally:
        if own: s.close()
