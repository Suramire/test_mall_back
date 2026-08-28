"""用户小程序 wx-login：租户路由、身份反查、配额和重复会员回归。"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.config import settings
from app.core.errors import BizCode
from app.core.tenant_context import reset, set_tenant
from app.models.mb_member import MbMember
from app.models.pf_tenant import PfTenant


def _enable_sqlite_autoincrement() -> None:
    """本模块单独执行时也让 BIGINT 主键在 SQLite 测试库自增。"""
    from sqlalchemy import BigInteger, Integer
    from app.db.base import Base
    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if isinstance(col.type, BigInteger):
                col.type = BigInteger().with_variant(Integer(), "sqlite")


_enable_sqlite_autoincrement()


@pytest.fixture(autouse=True)
def _fake_wechat_login(monkeypatch):
    monkeypatch.setattr(settings, "WECHAT_LOGIN_FAKE_ENABLED", True)


def _tenant(db_session, *, tenant_id: int, appid: str, status: str = "NORMAL", limit: int = 0, expired=False):
    tenant = PfTenant(
        id=tenant_id, tenant_no=f"WX{tenant_id}", name=f"wx租户{tenant_id}", status=status,
        wx_appid=appid, wx_secret_enc="test-secret-not-logged", member_limit=limit,
        expire_at=date.today() - timedelta(days=1) if expired else date.today() + timedelta(days=30),
    )
    db_session.add(tenant)
    db_session.commit()
    return tenant


def _login(client, appid: str, code: str):
    return client.post("/api/c/auth/wx-login", json={"appId": appid, "code": code})


def test_wx_login_creates_then_reuses_member_in_appid_tenant(client, db_session):
    _tenant(db_session, tenant_id=8101, appid="wx-test-8101")

    first = _login(client, "wx-test-8101", "fake:open-a").json()
    second = _login(client, "wx-test-8101", "fake:open-a").json()

    assert first["code"] == BizCode.OK
    assert second["code"] == BizCode.OK
    assert first["data"]["tenant"]["id"] == 8101
    assert first["data"]["member"]["id"] == second["data"]["member"]["id"]
    set_tenant(8101)
    try:
        assert db_session.query(MbMember).filter_by(tenant_id=8101, openid="fake-openid-open-a").count() == 1
    finally:
        reset()


def test_wx_login_rejects_unknown_appid_before_identity_exchange(client):
    body = _login(client, "wx-not-bound", "fake:any").json()
    assert body["code"] == BizCode.UNAUTHORIZED
    assert "AppID" in body["message"]


def test_wx_login_rejects_invalid_code(client, db_session):
    _tenant(db_session, tenant_id=8102, appid="wx-test-8102")
    body = _login(client, "wx-test-8102", "not-a-fake-code").json()
    assert body["code"] == BizCode.UNAUTHORIZED


@pytest.mark.parametrize(
    ("tenant_id", "status", "expired", "expected"),
    [(8103, "DISABLED", False, BizCode.TENANT_DISABLED), (8104, "NORMAL", True, BizCode.TENANT_EXPIRED)],
)
def test_wx_login_blocks_disabled_and_expired_tenants(client, db_session, tenant_id, status, expired, expected):
    _tenant(db_session, tenant_id=tenant_id, appid=f"wx-test-{tenant_id}", status=status, expired=expired)
    assert _login(client, f"wx-test-{tenant_id}", "fake:any").json()["code"] == expected


def test_wx_login_enforces_member_quota_only_for_new_openid(client, db_session):
    _tenant(db_session, tenant_id=8105, appid="wx-test-8105", limit=1)
    set_tenant(8105)
    try:
        db_session.add(MbMember(id=81050001, tenant_id=8105, member_no="WX81050001", openid="fake-openid-existing", nickname="已有会员"))
        db_session.commit()
    finally:
        reset()

    existing = _login(client, "wx-test-8105", "fake:existing").json()
    new_member = _login(client, "wx-test-8105", "fake:new").json()
    assert existing["code"] == BizCode.OK
    assert new_member["code"] == BizCode.TENANT_QUOTA_MEMBER
