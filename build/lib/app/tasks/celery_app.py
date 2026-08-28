"""Celery 应用与订单超时调度配置。

启动 worker：``celery -A app.tasks.celery_app worker -B``。
生产环境应通过环境变量覆盖 broker/backend，并由进程管理器托管。
"""
from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "mall",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks.order_timeout"],
)
celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "close-expired-orders-every-minute": {
            "task": "app.tasks.order_timeout.close_expired_orders_task",
            "schedule": 60.0,
        },
    },
)
