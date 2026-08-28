"""平台端验收：员工重置密码、平台敏感操作审计、会员手机号查看留痕。

覆盖：
- POST /api/pf/staff/{id}/reset-pwd：返回新密码、DB hash 变化、pwd_reset_required 持久化、
  旧密码登录失败、旧 access token 行为（JWT 无状态设计：不吊销，读代码确认）。
- 用重置后的新密码登录成功且 user.pwdResetRequired=True；自助改密后标记清零。
- 权限：无 PF_STAFF_RESET_PWD 权限码的平台 token / 商家端 token 调 reset-pwd 均被拒。
- 敏感操作审计：STAFF_RESET_PWD / STAFF_STATUS_CHANGE / ROLE_PERMS_CHANGE /
  PASSWORD_SELF_CHANGE 写入 pf_audit_log，GET /api/pf/audit 可按 action 读回，
  字段含操作人 ID / 操作人名 / 动作 / 时间。
- 会员手机号留痕：POST /api/pf/member/{id}/reveal-phone 返回明文手机号并写
  MEMBER_PHONE_VIEW 审计（tenantId 归属正确）。
"""
from __future__ import annotations

import uuid

from app.core import errors
from app.core.security import (
    SCOPE_MERCHANT,
    SCOPE_PLATFORM,
    create_access_token,
    hash_password,
    verify_password,
)
from app.core.tenant_context import reset, set_tenant
from app.models.mb_member import MbMember
from app.models.pf_role import PfRole
from app.models.pf_staff import PfStaff
from tests.conftest import assert_biz_code, assert_ok


def _enable_sqlite_autoincrement() -> None:
    from sqlalchemy import BigInteger, Integer

    from app.db.base import Base

    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if isinstance(col.type, BigInteger):
                col.type = BigInteger().with_variant(Integer(), "sqlite")


_enable_sqlite_autoincrement()

OPERATOR_OFFSET = 5
OPERATOR_NAME = "审计管理员"


def _pf_headers(perms: list[str], sub: int | None = None, base: int = 0) -> dict[str, str]:
    sid = sub if sub is not None else base + OPERATOR_OFFSET
    return {
        "Authorization": "Bearer "
        + create_access_token(subject=str(sid), scope=SCOPE_PLATFORM, perms=perms)
    }


def _seed_pf(db, base: int) -> tuple[int, int]:
    """建角色 + 目标员工 + 审计操作员（ID 与 token sub 对齐以便回查操作人名）。

    所有显式主键均由 base 派生且各用例 base 唯一，避免共享内存库中的主键冲突。
    """
    role_id = base + 1
    admin_id = base + OPERATOR_OFFSET
    target_id = base + 2
    db.add(PfRole(id=role_id, name=f"QA运营角色{base}", remark="qa", perms=["PF_STAFF"], is_system=0))
    db.add(PfStaff(
        id=admin_id, account=f"qapfadmin{base}", name=OPERATOR_NAME,
        password_hash=hash_password("Admin#123456"), phone="", role_id=role_id,
        status="ENABLED", pwd_reset_required=0,
    ))
    db.add(PfStaff(
        id=target_id, account=f"qapftarget{base}", name="被重置员工",
        password_hash=hash_password("OldPwd#123456"), phone="", role_id=role_id,
        status="ENABLED", pwd_reset_required=0,
    ))
    db.commit()
    return admin_id, target_id


def _get_staff(db, staff_id: int) -> PfStaff:
    from sqlalchemy import select

    return db.scalars(
        select(PfStaff).where(PfStaff.id == staff_id).execution_options(skip_tenant_filter=True)
    ).one()


def _audit_items(client, headers: dict[str, str], action: str) -> list[dict]:
    resp = client.get(f"/api/pf/audit?action={action}&size=50", headers=headers)
    body = assert_ok(resp)
    return body["data"]["list"]


ALL_PERMS = ["PF_STAFF", "PF_STAFF_RESET_PWD", "PF_ROLE", "PF_AUDIT_LOG", "PF_MEMBER_VIEW"]


class TestStaffResetPwd:
    def test_reset_pwd_success_persists_hash_and_force_flag(self, client, db_session):
        base = 91_000_000 + int(uuid.uuid4().int % 1_000_000) * 10
        _, target_id = _seed_pf(db_session, base)
        old_hash = _get_staff(db_session, target_id).password_hash
        old_token = create_access_token(str(target_id), SCOPE_PLATFORM, perms=["PF_STAFF"])

        resp = client.post(f"/api/pf/staff/{target_id}/reset-pwd", headers=_pf_headers(ALL_PERMS, base=base))
        body = assert_ok(resp)
        new_pwd = body["data"]["newPassword"]
        assert new_pwd and len(new_pwd) == 10 and new_pwd.isalnum()

        s = _get_staff(db_session, target_id)
        assert s.password_hash != old_hash
        assert verify_password(new_pwd, s.password_hash)
        assert not verify_password("OldPwd#123456", s.password_hash)
        assert s.pwd_reset_required == 1

        # 重置密码后旧 token 必须立即失效（token 版本吊销）
        r = client.get("/api/pf/auth/me", headers={"Authorization": f"Bearer {old_token}"})
        assert_biz_code(r, errors.BizCode.UNAUTHORIZED)

        r = client.post("/api/pf/auth/login", json={"account": f"qapftarget{base}", "password": "OldPwd#123456"})
        assert_biz_code(r, errors.BizCode.UNAUTHORIZED)

    def test_login_with_reset_pwd_and_force_change_flow(self, client, db_session):
        base = 92_000_000 + int(uuid.uuid4().int % 1_000_000) * 10
        _, target_id = _seed_pf(db_session, base)
        account = f"qapftarget{base}"

        body = assert_ok(client.post(
            f"/api/pf/staff/{target_id}/reset-pwd",
            headers=_pf_headers(["PF_STAFF_RESET_PWD"], base=base),
        ))
        new_pwd = body["data"]["newPassword"]

        login = assert_ok(client.post("/api/pf/auth/login", json={"account": account, "password": new_pwd}))
        tokens = login["data"]
        assert tokens["accessToken"] and tokens["refreshToken"]
        assert tokens["user"]["pwdResetRequired"] is True

        change = assert_ok(client.put(
            "/api/pf/auth/password",
            headers={"Authorization": f"Bearer {tokens['accessToken']}"},
            json={"oldPassword": new_pwd, "newPassword": "SelfSet#123", "confirmPassword": "SelfSet#123"},
        ))
        assert "重新登录" in change["message"]

        s = _get_staff(db_session, target_id)
        assert s.pwd_reset_required == 0
        assert verify_password("SelfSet#123", s.password_hash)

        relogin = assert_ok(client.post("/api/pf/auth/login", json={"account": account, "password": "SelfSet#123"}))
        assert relogin["data"]["user"]["pwdResetRequired"] is False

    def test_reset_missing_staff_not_found(self, client):
        base = 93_000_000 + int(uuid.uuid4().int % 1_000_000) * 10
        resp = client.post(f"/api/pf/staff/{base}/reset-pwd", headers=_pf_headers(["PF_STAFF_RESET_PWD"]))
        assert_biz_code(resp, errors.BizCode.NOT_FOUND)


class TestResetPwdPermission:
    def test_platform_token_without_perm_rejected(self, client, db_session):
        base = 94_000_000 + int(uuid.uuid4().int % 1_000_000) * 10
        _, target_id = _seed_pf(db_session, base)
        resp = client.post(
            f"/api/pf/staff/{target_id}/reset-pwd",
            headers=_pf_headers(["PF_DASHBOARD"], sub=base),
        )
        assert_biz_code(resp, errors.BizCode.FORBIDDEN)

    def test_merchant_scope_rejected_on_pf_path(self, client, db_session):
        base = 95_000_000 + int(uuid.uuid4().int % 1_000_000) * 10
        _, target_id = _seed_pf(db_session, base)
        merchant_h = {
            "Authorization": "Bearer "
            + create_access_token("10", SCOPE_MERCHANT, tenant_id=1001, perms=["MC_ALL"])
        }
        resp = client.post(f"/api/pf/staff/{target_id}/reset-pwd", headers=merchant_h)
        assert_biz_code(resp, errors.BizCode.FORBIDDEN)


class TestSensitiveOpAudit:
    def test_reset_pwd_and_status_change_audited_readable(self, client, db_session):
        base = 96_000_000 + int(uuid.uuid4().int % 1_000_000) * 10
        _, target_id = _seed_pf(db_session, base)
        admin_h = _pf_headers(ALL_PERMS, base=base)

        assert_ok(client.post(f"/api/pf/staff/{target_id}/reset-pwd", headers=admin_h))
        toggle = assert_ok(client.post(f"/api/pf/staff/{target_id}/toggle-status", headers=admin_h))
        assert toggle["data"]["status"] == "DISABLED"

        rows = _audit_items(client, admin_h, "STAFF_RESET_PWD")
        row = next(r for r in rows if r["targetId"] == str(target_id))
        assert row["operatorId"] == base + OPERATOR_OFFSET
        assert row["operatorName"] == OPERATOR_NAME
        assert row["action"] == "STAFF_RESET_PWD"
        assert row["targetType"] == "pf_staff"
        assert row["createdAt"]
        assert "ip" in row
        assert row["detail"]["after"]["pwd_reset_required"] == 1

        status_rows = _audit_items(client, admin_h, "STAFF_STATUS_CHANGE")
        srow = next(r for r in status_rows if r["targetId"] == str(target_id))
        assert srow["detail"]["after"]["status"] == "DISABLED"

    def test_role_perms_change_audited(self, client, db_session):
        base = 97_000_000 + int(uuid.uuid4().int % 1_000_000) * 10
        _, target_id = _seed_pf(db_session, base)
        role_id = base + 1
        admin_h = _pf_headers(ALL_PERMS, base=base)

        assert_ok(client.put(f"/api/pf/role/{role_id}/perms", headers=admin_h, json=["PF_STAFF"]))
        rows = _audit_items(client, admin_h, "ROLE_PERMS_CHANGE")
        row = next(r for r in rows if r["targetId"] == str(role_id))
        assert row["operatorName"] == OPERATOR_NAME
        assert row["detail"]["after"]["perms"] == ["PF_STAFF"]
        assert target_id

    def test_operator_name_falls_back_to_db_when_context_missing(self, client, db_session):
        """中间件只注入 sub 不注入姓名时，审计应回查员工表而非一律 system。"""
        base = 98_000_000 + int(uuid.uuid4().int % 1_000_000) * 10
        _, target_id = _seed_pf(db_session, base)
        assert_ok(client.post(
            f"/api/pf/staff/{target_id}/reset-pwd",
            headers=_pf_headers(["PF_STAFF_RESET_PWD"], base=base),
        ))
        from sqlalchemy import select

        from app.models.pf_audit_log import PfAuditLog

        row = db_session.scalars(
            select(PfAuditLog).where(
                PfAuditLog.action == "STAFF_RESET_PWD", PfAuditLog.target_id == str(target_id)
            ).execution_options(skip_tenant_filter=True)
        ).first()
        assert row is not None
        assert row.operator_name == OPERATOR_NAME


class TestMemberPhoneAudit:
    def test_reveal_phone_returns_plaintext_and_writes_audit(self, client, db_session):
        base = 99_000_000 + int(uuid.uuid4().int % 1_000_000) * 10
        _, _admin = _seed_pf(db_session, base)
        member_id = base + 7
        set_tenant(1001)
        try:
            db_session.add(MbMember(
                id=member_id, tenant_id=1001, member_no=f"QAPF{base}",
                nickname="敏感会员", phone_enc="13800009999", phone_mask="138****9999",
                phone_hash="qa-hash",
            ))
            db_session.commit()
        finally:
            reset()

        admin_h = _pf_headers(ALL_PERMS, base=base)

        detail = assert_ok(client.get(f"/api/pf/member/{member_id}", headers=admin_h))
        assert detail["data"]["phoneMask"] == "138****9999"
        assert "phone" not in detail["data"] or detail["data"].get("phone") is None

        reveal = assert_ok(client.post(f"/api/pf/member/{member_id}/reveal-phone", headers=admin_h))
        assert reveal["data"]["phone"] == "13800009999"

        rows = _audit_items(client, admin_h, "MEMBER_PHONE_VIEW")
        row = next(r for r in rows if r["targetId"] == str(member_id))
        assert row["action"] == "MEMBER_PHONE_VIEW"
        assert row["tenantId"] == 1001
        assert row["operatorName"] == OPERATOR_NAME
        assert row["createdAt"]

    def test_reveal_phone_requires_perm(self, client, db_session):
        base = 89_000_000 + int(uuid.uuid4().int % 1_000_000) * 10
        member_id = base + 8
        set_tenant(1001)
        try:
            db_session.add(MbMember(
                id=member_id, tenant_id=1001, member_no=f"QAPFN{base}",
                nickname="无权会员", phone_enc="13900008888", phone_mask="139****8888",
            ))
            db_session.commit()
        finally:
            reset()
        resp = client.post(
            f"/api/pf/member/{member_id}/reveal-phone",
            headers=_pf_headers(["PF_DASHBOARD"], sub=base),
        )
        assert_biz_code(resp, errors.BizCode.FORBIDDEN)


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
