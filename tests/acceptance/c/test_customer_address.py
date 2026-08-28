"""用户地址簿真实路由回归：默认地址与跨会员隔离。"""
from __future__ import annotations

from app.core.errors import BizCode
from app.core.security import SCOPE_CUSTOMER, create_access_token
from app.core.tenant_context import reset, set_tenant
from app.models.mb_member import MbMember
from app.models.pf_tenant import PfTenant
from tests.conftest import assert_biz_code


def _enable_sqlite_autoincrement() -> None:
    """让产品的 BIGINT 主键在 SQLite 验收库中按 INTEGER PRIMARY KEY 自增。"""
    from sqlalchemy import BigInteger, Integer

    from app.db.base import Base

    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if isinstance(col.type, BigInteger):
                col.type = BigInteger().with_variant(Integer(), "sqlite")


# 根夹具创建 SQLite 表前必须完成类型映射；生产 MySQL DDL 不受影响。
_enable_sqlite_autoincrement()


def _headers(member_id: int, tenant_id: int = 1001) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + create_access_token(
            str(member_id), SCOPE_CUSTOMER, tenant_id=tenant_id
        )
    }


def _seed_members(db_session) -> None:
    """准备两个同租户会员；请求中间件也会查询租户状态。"""
    db_session.add_all([
        PfTenant(id=1001, tenant_no="QA1001", name="QA地址租户", status="NORMAL"),
        PfTenant(id=2002, tenant_no="QA2002", name="QA地址跨租户", status="NORMAL"),
    ])
    set_tenant(1001)
    try:
        db_session.add_all([
            MbMember(id=11001, tenant_id=1001, member_no="QAADDR1", nickname="QA会员1"),
            MbMember(id=11002, tenant_id=1001, member_no="QAADDR2", nickname="QA会员2"),
        ])
        db_session.commit()
    finally:
        reset()


def _create(client, headers: dict[str, str], name: str, is_default: bool | None = None) -> int:
    body = {"receiverName": name, "phone": "13800000000", "detail": name}
    if is_default is not None:
        body["isDefault"] = is_default
    return assert_biz_code(client.post("/api/c/address", headers=headers, json=body), BizCode.OK)["data"]["id"]


def test_customer_address_default_switch_delete_and_isolation(client, db_session):
    _seed_members(db_session)
    h1, h2 = _headers(11001), _headers(11002)

    first = _create(client, h1, "QA第一地址")
    second = _create(client, h1, "QA第二地址", is_default=False)
    rows = assert_biz_code(client.get("/api/c/address", headers=h1), BizCode.OK)["data"]
    assert [row["id"] for row in rows if row["isDefault"]] == [first]

    assert_biz_code(client.put(f"/api/c/address/{second}/default", headers=h1), BizCode.OK)
    rows = assert_biz_code(client.get("/api/c/address", headers=h1), BizCode.OK)["data"]
    assert [row["id"] for row in rows if row["isDefault"]] == [second]

    # 详情仅本人可读；其他会员不能读写该地址。
    assert_biz_code(client.get(f"/api/c/address/{second}", headers=h1), BizCode.OK)
    assert_biz_code(client.get(f"/api/c/address/{second}", headers=h2), BizCode.NOT_FOUND)
    assert_biz_code(
        client.put(f"/api/c/address/{second}", headers=h2, json={"detail": "越权"}),
        BizCode.NOT_FOUND,
    )

    # 删除当前默认地址应自动提升其余有效地址；删最后一条后列表为空且详情不可读。
    assert_biz_code(client.delete(f"/api/c/address/{second}", headers=h1), BizCode.OK)
    rows = assert_biz_code(client.get("/api/c/address", headers=h1), BizCode.OK)["data"]
    assert len(rows) == 1 and rows[0]["id"] == first and rows[0]["isDefault"] is True
    assert_biz_code(client.delete(f"/api/c/address/{first}", headers=h1), BizCode.OK)
    assert assert_biz_code(client.get("/api/c/address", headers=h1), BizCode.OK)["data"] == []
    assert_biz_code(client.get(f"/api/c/address/{first}", headers=h1), BizCode.NOT_FOUND)
