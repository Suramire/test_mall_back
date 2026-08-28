"""剩余商家 mutation 的前置权限契约。"""
import pytest
from sqlalchemy.orm import sessionmaker

from app.core.security import SCOPE_MERCHANT, create_access_token
from app.models.mc_config import McRole
from app.models.mc_staff import McStaff

from .conftest import TENANT_A, assert_code, assert_envelope


def _headers(perms):
    token = create_access_token(subject="10", scope=SCOPE_MERCHANT, tenant_id=TENANT_A, perms=perms)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("method,path", [
    ("post", "/api/mc/order/999999/ship"),
    ("post", "/api/mc/refund/999999/approve"),
    ("put", "/api/mc/msg-config/999999"),
    ("put", "/api/mc/shop"),
    ("post", "/api/mc/freight-template"),
    ("put", "/api/mc/level/999999"),
    ("put", "/api/mc/points/rule"),
])
def test_remaining_mutations_reject_before_business_validation(client, engine, seed_tenants, method, path):
    Session = sessionmaker(bind=engine)
    from app.core.tenant_context import reset, set_tenant
    set_tenant(TENANT_A)
    with Session() as session:
        role = McRole(tenant_id=TENANT_A, name=f"守卫{path[-8:]}", remark="", perms=[])
        session.add(role)
        session.flush()
        staff = session.get(McStaff, 10)
        staff.role_id = role.id
        staff.is_admin = 0
        session.commit()
    reset()
    response = getattr(client, method)(path, headers=_headers([]), json={})
    assert assert_code(response, 40301)["code"] == 40301
    # MC_ALL 只验证已通过权限守卫，后续业务可返回参数/不存在等业务错误。
    response = getattr(client, method)(path, headers=_headers(["MC_ALL"]), json={})
    assert assert_envelope(response)["code"] != 40301
