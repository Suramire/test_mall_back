"""商家端关键 mutation 必须在请求体校验前执行权限守卫。"""
from sqlalchemy.orm import sessionmaker

from app.core.security import SCOPE_MERCHANT, create_access_token
from app.models.mc_config import McRole
from app.models.mc_staff import McStaff

from .conftest import TENANT_A, assert_code


def test_mutations_preflight_forbidden_and_mc_all_allowed(client, engine, seed_tenants):
    Session = sessionmaker(bind=engine)
    from app.core.tenant_context import reset, set_tenant
    set_tenant(TENANT_A)
    with Session() as session:
        role = McRole(tenant_id=TENANT_A, name="前置守卫QA", remark="", perms=[])
        session.add(role)
        session.flush()
        staff = session.get(McStaff, 10)
        staff.role_id = role.id
        staff.is_admin = 0
        session.commit()
    reset()
    token = create_access_token(subject="10", scope=SCOPE_MERCHANT, tenant_id=TENANT_A, perms=[])
    headers = {"Authorization": f"Bearer {token}"}
    cases = [
        ("/api/mc/goods", "post"), ("/api/mc/staff", "post"),
        ("/api/mc/role", "post"), ("/api/mc/points/adjust", "post"),
        ("/api/mc/verify", "post"), ("/api/mc/store", "post"),
    ]
    for path, method in cases:
        assert assert_code(getattr(client, method)(path, headers=headers, json={}), 40301)["code"] == 40301
    # MC_ALL 管理员兼容路径：即使 payload 不完整，也应越过权限守卫进入业务校验。
    admin_token = create_access_token(subject="10", scope=SCOPE_MERCHANT, tenant_id=TENANT_A,
                                      perms=["MC_ALL"])
    result = client.post("/api/mc/store", headers={"Authorization": f"Bearer {admin_token}"}, json={})
    assert assert_code(result, 0)["code"] == 0
