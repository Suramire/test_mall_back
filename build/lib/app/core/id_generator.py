"""编号生成器。

用 Redis INCR 生成递增序号（带前缀+日期），Redis 不可用时降级到 MySQL pf_sequence。
租户编号：M + 5位，如 M10001。
"""
from __future__ import annotations

import datetime
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.redis import get_redis

logger = logging.getLogger(__name__)


def _redis_incr(key: str) -> int:
    return int(get_redis().incr(key))


def _biz_date(tz: str | None = None) -> str:
    """业务本地日期字符串 YYYYMMDD。tz 为 IANA 时区名，失败降级 UTC。"""
    try:
        zone = ZoneInfo(tz) if tz else ZoneInfo(settings.ORDER_NO_TZ)
    except (KeyError, OSError):
        logger.warning("无效时区 %s，单号日期降级为 UTC", tz)
        zone = ZoneInfo("UTC")
    return datetime.datetime.now(zone).strftime("%Y%m%d")


def _mysql_incr(session: Session, seq_key: str) -> int:
    """MySQL pf_sequence 兜底：INSERT ... ON DUPLICATE KEY UPDATE current_val=current_val+1。"""
    stmt = text(
        "INSERT INTO pf_sequence (seq_key, current_val) "
        "VALUES (:k, 1) "
        "ON DUPLICATE KEY UPDATE current_val = current_val + 1"
    )
    session.execute(stmt, {"k": seq_key})
    row = session.execute(
        text("SELECT current_val FROM pf_sequence WHERE seq_key = :k"), {"k": seq_key}
    ).one()
    return row.current_val


def next_tenant_no(session: Session) -> str:
    """生成租户编号 M10001。"""
    try:
        val = _redis_incr("seq:tenant_no")
    except Exception:
        val = _mysql_incr(session, "tenant")
    return f"M{val + 10000:05d}"


def next_member_no(session: Session, tenant_id: int) -> str:
    """会员编号 XF + 6位，租户内递增。"""
    key = f"seq:t{tenant_id}:member"
    try:
        val = _redis_incr(key)
    except Exception:
        val = _mysql_incr(session, f"t{tenant_id}:member")
    return f"XF{val:06d}"


def next_order_no(session: Session, tenant_id: int, prefix: str = "ORD") -> str:
    """订单编号 ORD/PT + YYYYMMDD + 6位，租户+日递增。"""
    today = _biz_date()
    key = f"seq:t{tenant_id}:order:{today}"
    try:
        val = _redis_incr(key)
    except Exception:
        val = _mysql_incr(session, f"t{tenant_id}:order:{today}")
    return f"{prefix}{today}{val:06d}"


def next_refund_no(session: Session, tenant_id: int) -> str:
    return next_order_no(session, tenant_id, prefix="RF")
