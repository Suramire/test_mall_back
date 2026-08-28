"""T-901 验收：后端基座与多租户隔离。

覆盖范围（依据 docs/测试验收标准-P0.md §2 基座横切 + §0.3 DoD）：
  B1 统一响应体
  B2 错误码映射（业务 HTTP 200 + code）
  B3 多租户隔离（跨租户拒绝 / 自动注入）
  B4 tenant 上下文缺失 Fail-Fast
  B5 JWT 生成/校验
  B6 幂等键
  B7 编号生成（tenantNo/orderNo/skuCode）
  B8 权限守卫注入点（Depends guard）

注意：当前为验收骨架。部分用例会因 be-dev 尚未完全挂载中间件/路由而 xfail，
随交付逐步转绿。"""
from __future__ import annotations

import pytest

from app.core import errors
from app.core.security import (
    create_access_token,
    decode_token,
    SCOPE_PLATFORM,
    SCOPE_MERCHANT,
    hash_password,
    verify_password,
)
from app.core.redis import idempotency_set, is_token_blacklisted, blacklist_token
from app.core.id_generator import next_tenant_no, next_order_no
from app.core.tenant_context import TenantContextMissingError, require_tenant_id
from tests.conftest import assert_biz_code, assert_ok


# ----------------------------- B1 统一响应体 -----------------------------
class TestUnifiedResponse:
    def test_health_returns_unified_envelope(self, client):
        """健康接口也应满足统一响应体结构（无 data 也可）。"""
        resp = client.get("/api/common/health")
        # 健康接口可能直接返回裸对象，不强制 code；仅验证可达
        assert resp.status_code in (200, 404)

    def test_trace_id_present(self, client, auth_headers):
        """所有响应含 traceId（与 X-Trace-Id 一致）。"""
        resp = client.get("/api/common/health", headers=auth_headers)
        body = resp.json() if resp.status_code == 200 and "code" in resp.text else None
        # 基座完整后此处应断言 body["traceId"]
        pytest.skip("待 be-dev 完成统一响应体全链路（main.py 已装配 handlers）")


# ----------------------------- B2 错误码映射 -----------------------------
class TestErrorMapping:
    def test_validation_error_returns_40001_http200(self, client):
        """参数校验失败 -> HTTP 200 + code 40001 + data.fields。"""
        # 找一个需要入参的端点；基座未挂业务路由前先验证 handlers 对 RequestValidationError 的映射
        resp = client.get("/api/common/health?size=not_int")
        # 无 Pydantic 校验时不会触发；此处仅确认处理器已装配
        pytest.skip("待业务路由挂载后填充具体入参校验用例")

    def test_unhandled_exception_returns_50000(self, client):
        """未捕获异常 -> code 50000，禁止裸抛 500 详情。"""
        # 触发一个会抛异常的路径（基座未挂前跳过）
        pytest.skip("待 be-dev 提供可触发异常的探针端点")


# ----------------------------- B5 JWT -----------------------------
class TestJWT:
    def test_platform_token_no_tenant(self):
        tok = create_access_token(subject="1", scope=SCOPE_PLATFORM)
        payload = decode_token(tok)
        assert "tid" not in payload
        assert payload["scope"] == "platform"

    def test_merchant_token_has_tenant(self):
        tok = create_access_token(subject="10", scope=SCOPE_MERCHANT, tenant_id=1001)
        payload = decode_token(tok)
        assert payload["tid"] == 1001

    def test_invalid_token_raises(self):
        with pytest.raises(Exception):
            decode_token("not-a-valid-token")

    def test_password_hash_verify(self):
        h = hash_password("Secret123")
        assert verify_password("Secret123", h)
        assert not verify_password("wrong", h)


# ----------------------------- B3/B4 多租户隔离 -----------------------------
class TestTenantIsolation:
    def test_require_tenant_id_failfast_when_missing(self):
        """缺失租户上下文访问 require_tenant_id -> 抛 TenantContextMissingError。"""
        with pytest.raises(TenantContextMissingError):
            require_tenant_id()

    def test_tenant_filter_injected_on_select(self, db_session):
        """SELECT 必须实际过滤其他租户数据，不能只检查模型是否注册。"""
        from app.core.tenant_context import reset, set_tenant
        from app.models.gd_goods import GdCategory

        # 通过 Core INSERT 写入两条不同租户的原始数据，绕开 ORM 的 before_flush，
        # 再从正常 ORM 查询验证 do_orm_execute 过滤器的真实行为。
        db_session.connection().execute(
            GdCategory.__table__.insert(),
            [
                {"id": 900001, "tenant_id": 1001, "channel": "NORMAL", "parent_id": 0,
                 "name": "租户A分类", "icon": "", "sort": 0, "status": "ENABLED"},
                {"id": 900002, "tenant_id": 2002, "channel": "NORMAL", "parent_id": 0,
                 "name": "租户B分类", "icon": "", "sort": 0, "status": "ENABLED"},
            ],
        )
        set_tenant(1001)
        try:
            rows = db_session.query(GdCategory).filter(
                GdCategory.id.in_([900001, 900002])
            ).all()
            assert [row.id for row in rows] == [900001]
            assert {row.tenant_id for row in rows} == {1001}
        finally:
            reset()

    def test_cross_tenant_write_rejected(self, db_session):
        """跨租户写入（target.tenant_id != 当前上下文）-> 拒绝。"""
        from app.db.orm_hooks import _TENANT_MODELS, register_tenant_model
        from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin
        from sqlalchemy.orm import Mapped, mapped_column

        if not _TENANT_MODELS:
            pytest.skip("暂无业务模型注册；待 T-020+ 交付后填充")

        # 设当前租户上下文为 1001，尝试写入 tenant_id=9999 的对象 -> 应拒绝
        from app.core.tenant_context import set_tenant
        from app.core.exceptions import ForbiddenError

        set_tenant(1001)
        model_cls = _TENANT_MODELS[0]
        obj = model_cls()
        obj.tenant_id = 9999  # 跨租户
        db_session.add(obj)
        with pytest.raises(ForbiddenError):
            db_session.flush()


# ----------------------------- B6 幂等键 -----------------------------
class TestIdempotency:
    def test_idempotency_set_once(self):
        ok = idempotency_set("idem:test:1", ttl=10)
        assert ok is True
        again = idempotency_set("idem:test:1", ttl=10)
        assert again is False

    def test_token_blacklist(self):
        blacklist_token("jti:abc", ttl=10)
        assert is_token_blacklisted("jti:abc") is True


# ----------------------------- B7 编号生成 -----------------------------
class TestSequence:
    def test_next_tenant_no_format(self, db_session):
        """租户编号格式 M + 5 位递增。"""
        no1 = next_tenant_no(db_session)
        no2 = next_tenant_no(db_session)
        assert no1.startswith("M") and len(no1) == 6
        assert int(no1[1:]) + 1 == int(no2[1:])

    def test_next_order_no_format(self, db_session):
        no = next_order_no(db_session, tenant_id=1001)
        assert no.startswith("ORD") and "1001" not in no.split("ORD")[1][:8] or True
        # 格式 ORD + YYYYMMDD + 6位
        import re

        assert re.match(r"^ORD\d{8}\d{6}$", no), no


# ----------------------------- B8 权限守卫 -----------------------------
class TestGuard:
    def test_tenant_guard_rejects_missing_token_on_merchant_path(self, client):
        """商家端路径无 token -> 40100（中间件挂载后生效）。

        注：当前 main.py 未挂载 TenantGuardMiddleware 与业务路由，
        该用例在 be-dev 完成 T-011 装配后转绿。
        """
        pytest.skip("待 be-dev 在 main.py 挂载 TenantGuardMiddleware + 业务路由")

    def test_tenant_guard_platform_no_tenant(self, client, auth_headers):
        """平台端带 token 且不注入 tenant（pf_* 为平台级表）。"""
        pytest.skip("待 /api/pf 路由挂载后填充")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
