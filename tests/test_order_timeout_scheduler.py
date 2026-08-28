from app.tasks.celery_app import celery_app
from app.tasks.order_timeout import close_expired_orders_task


def test_order_timeout_has_celery_beat_schedule():
    entry = celery_app.conf.beat_schedule["close-expired-orders-every-minute"]
    assert entry["task"] == "app.tasks.order_timeout.close_expired_orders_task"
    assert entry["schedule"] == 60.0


def test_order_timeout_task_is_registered_and_directly_callable():
    assert close_expired_orders_task.name in celery_app.tasks
    assert close_expired_orders_task.run.__name__ == "close_expired_orders_task"
