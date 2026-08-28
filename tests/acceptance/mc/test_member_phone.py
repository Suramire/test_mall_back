"""商家会员手机号最小权限与审计回归。"""
from app.core.security import SCOPE_MERCHANT, create_access_token
from app.models.mb_member import MbMember
from app.models.mc_staff import McStaff
from app.models.pf_audit_log import PfAuditLog

from .conftest import TENANT_A, TENANT_B, assert_code

MC = "/api/mc"


def _headers(subject="10", tenant=TENANT_A, perms=None):
    token = create_access_token(subject=subject, scope=SCOPE_MERCHANT,
                                tenant_id=tenant, perms=perms or [])
    return {"Authorization": f"Bearer {token}"}


def test_member_phone_requires_permission_and_writes_audit(client, app, engine, seed_tenants):
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=engine)
    from app.core.tenant_context import reset, set_tenant
    set_tenant(TENANT_A)
    with Session() as session:
        member = session.get(MbMember, seed_tenants["member_a"])
        member.phone_enc = "13800001111"
        session.commit()
    reset()

    response = client.get(f"{MC}/member/{seed_tenants['member_a']}/phone",
                          headers=_headers(perms=["MEMBER_PHONE_FULL"]))
    body = assert_code(response, 0)
    assert body["data"]["phone"] == "13800001111"

    with Session() as session:
        audit = session.query(PfAuditLog).filter_by(
            tenant_id=TENANT_A, action="MEMBER_PHONE_VIEW",
            target_id=str(seed_tenants["member_a"]),
        ).order_by(PfAuditLog.id.desc()).first()
        assert audit is not None
        assert audit.operator_id == 10
        assert audit.operator_name == "店主1"


def test_member_phone_without_permission_is_forbidden(client, seed_tenants):
    response = client.get(f"{MC}/member/{seed_tenants['member_a']}/phone",
                          headers=_headers())
    assert assert_code(response, 40301)["code"] == 40301


def test_member_phone_cross_tenant_is_not_found(client, seed_tenants):
    response = client.get(f"{MC}/member/{seed_tenants['member_b']}/phone",
                          headers=_headers(perms=["MEMBER_PHONE_FULL"]))
    assert assert_code(response, 40400)["code"] == 40400


def test_disabled_staff_token_is_revoked_and_reenabled_token_recovers(client, engine, seed_tenants):
    """真实员工状态变更后旧 token 立即失效，重新启用后按现行策略恢复。"""
    from sqlalchemy.orm import sessionmaker

    login = client.post(f"{MC}/auth/login", json={"account": "shopA", "password": "Secret123"})
    token = assert_code(login, 0)["data"]["accessToken"]
    headers = {"Authorization": f"Bearer {token}"}
    Session = sessionmaker(bind=engine)
    from app.core.tenant_context import reset, set_tenant
    set_tenant(TENANT_A)
    with Session() as session:
        staff = session.get(McStaff, seed_tenants["staff_a"])
        staff.status = "DISABLED"
        session.commit()
    reset()
    assert assert_code(client.get(f"{MC}/auth/me", headers=headers), 40100)["code"] == 40100
    set_tenant(TENANT_A)
    with Session() as session:
        staff = session.get(McStaff, seed_tenants["staff_a"])
        staff.status = "ENABLED"
        session.commit()
    reset()
    assert assert_code(client.get(f"{MC}/auth/me", headers=headers), 0)["code"] == 0
