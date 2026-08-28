"""订单超时关单 cron 入口。

示例：``* * * * * cd /path/to/mall/backend && .venv/bin/python scripts/close_expired_orders.py``。
任务本身按订单状态和截止时间幂等，失败返回非零码供 cron 告警。
"""
from app.tasks.order_timeout import close_expired_orders


if __name__ == "__main__":
    print(f"closed_expired_orders={close_expired_orders()}")
