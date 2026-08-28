"""订单号/退款号内嵌日期应使用业务本地时区（默认 Asia/Shanghai），不依赖进程时区。

用固定"此刻"穿透 _biz_date 的 datetime.now(zone)，覆盖：
① 普通生成包含正确时区日期（进程为 UTC 时仍取上海自然日）；
② 时区可配置（改成 America/New_York 时日期不同）；
③ 无效时区降级 UTC 并打印 warning，不静默出错。
"""
from __future__ import annotations

import datetime

import pytest

from app.core import id_generator
from app.core.config import settings


class _FixedNow:
    """固定"此刻"：UTC 2026-08-27 16:30:00。
    = 上海 2026-08-28 00:30（跨日）
    = 纽约 2026-08-27 12:30（未跨日）
    """

    _FIXED = datetime.datetime(2026, 8, 27, 16, 30, 0, tzinfo=datetime.UTC)

    @classmethod
    def now(cls, tz: datetime.tzinfo | None = None) -> datetime.datetime:
        dt = cls._FIXED
        if tz is not None:
            dt = dt.astimezone(tz)
        return dt


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(id_generator.datetime, "datetime", _FixedNow)


def test_order_no_embeds_shanghai_date_under_utc_process(fixed_clock, monkeypatch, db_session):
    monkeypatch.setattr(settings, "ORDER_NO_TZ", "Asia/Shanghai")
    no = id_generator.next_order_no(db_session, tenant_id=1001)
    assert "20260828" in no, f"应内嵌上海自然日 20260828，实际 {no}"
    refund = id_generator.next_refund_no(db_session, tenant_id=1001)
    assert "20260828" in refund, f"退款号应同源生效，实际 {refund}"


def test_timezone_configurable_new_york(fixed_clock, monkeypatch, db_session):
    monkeypatch.setattr(settings, "ORDER_NO_TZ", "America/New_York")
    no = id_generator.next_order_no(db_session, tenant_id=1001)
    assert "20260827" in no, f"纽约时区应为 20260827，实际 {no}"
    assert "20260828" not in no


def test_invalid_timezone_falls_back_to_utc(fixed_clock, monkeypatch, caplog, db_session):
    import logging

    monkeypatch.setattr(settings, "ORDER_NO_TZ", "Invalid/Zone")
    with caplog.at_level(logging.WARNING, logger=id_generator.logger.name):
        date_str = id_generator._biz_date()
    # 无效时区降级 UTC：固定此刻 UTC 为 2026-08-27
    assert date_str == "20260827", f"降级 UTC 应为 20260827，实际 {date_str}"
    assert any("降级" in r.message for r in caplog.records), "应打印降级 warning"


def test_biz_date_matches_real_zoneinfo(monkeypatch):
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(settings, "ORDER_NO_TZ", "Asia/Shanghai")
    assert id_generator._biz_date() == datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
