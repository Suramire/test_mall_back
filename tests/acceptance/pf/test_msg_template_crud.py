from app.core.security import SCOPE_PLATFORM, create_access_token
from app.models.pf_msg_template import PfMsgTemplate
from tests.conftest import assert_biz_code


def _enable_sqlite_autoincrement():
    from sqlalchemy import BigInteger, Integer
    from app.db.base import Base
    for table in Base.metadata.tables.values():
        for col in table.primary_key.columns:
            if isinstance(col.type, BigInteger):
                col.type = BigInteger().with_variant(Integer(), "sqlite")

_enable_sqlite_autoincrement()


def _headers(perms):
    return {"Authorization": "Bearer " + create_access_token(subject="1", scope=SCOPE_PLATFORM, perms=perms)}


def test_platform_message_template_crud_audit_and_soft_delete(client, db_session):
    db_session.add(PfMsgTemplate(id=1, template_no="ORDER_PAID", name="系统模板", channel="WX", content="系统", status="ENABLED"))
    db_session.commit()
    denied = client.post("/api/pf/msg-template", headers=_headers([]), json={})
    assert assert_biz_code(denied, 40301)["code"] == 40301
    h = _headers(["PF_MSG_TEMPLATE"])
    payload = {"templateNo": "QA_CRUD_01", "name": "QA消息", "channel": "WX", "content": "订单#{orderNo}"}
    created = assert_biz_code(client.post("/api/pf/msg-template", headers=h, json=payload), 0)["data"]
    tid = created["id"]
    assert assert_biz_code(client.post("/api/pf/msg-template", headers=h, json=payload), 40900)["code"] == 40900
    assert assert_biz_code(client.put(f"/api/pf/msg-template/{tid}", headers=h, json={"name": "QA消息改"}), 0)["code"] == 0
    listed = assert_biz_code(client.get("/api/pf/msg-template", headers=h), 0)["data"]
    assert next(x for x in listed if x["id"] == tid)["name"] == "QA消息改"
    status = assert_biz_code(client.post(f"/api/pf/msg-template/{tid}/toggle-status", headers=h), 0)["data"]
    assert status["status"] == "DISABLED"
    assert assert_biz_code(client.delete("/api/pf/msg-template/1", headers=h), 40301)["code"] == 40301
    assert assert_biz_code(client.delete(f"/api/pf/msg-template/{tid}", headers=h), 0)["code"] == 0
    assert all(x["id"] != tid for x in assert_biz_code(client.get("/api/pf/msg-template", headers=h), 0)["data"])
    row = db_session.get(PfMsgTemplate, tid)
    assert row is not None and row.deleted_at is not None
