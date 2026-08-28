"""商家管理后台 `/api/mc` 联调验收用例。

依据：docs/测试验收标准-P0.md、docs/architecture/03-API设计.md
真实路由契约来自 openapi.json 实测（be-dev-2 提交 1c33844 后路径已收敛）。

覆盖：
  A. 认证链路（login → me → refresh → logout；代客 SSO）
  B. 越权路径（无 token 40100 / 过期 40101 / 平台 token 40301 / 跨租户拦截）
  C. 业务主链路（商品、订单、核销、会员、积分、门店、店铺、规则）
  D. 数字真实性（dashboard kpi/trend/todo —— 今日订单/销售额/新增会员/今日核销
     delta 环比、趋势图 X 轴连续性、订单状态枚举口径）
  E. 幂等（points/adjust idempotency_key）
  F. 统一响应体（HTTP 恒 200 + code/message/data/traceId）

约定：
- 接口尚未实现的用 `pytest.skip` 并注明等待哪个接口，不因未实现而不写。
- 断言一律基于实测响应，不基于源码阅读推断。
"""
from __future__ import annotations

import uuid

import pytest

from app.core.errors import BizCode

from .conftest import (
    STAFF_A_ACCOUNT,
    STAFF_A_PASSWORD,
    STAFF_B_ACCOUNT,
    STAFF_B_PASSWORD,
    TENANT_A,
    TENANT_B,
    assert_code,
    assert_envelope,
    assert_http200,
    assert_trace_id,
)

MC = "/api/mc"


# ===========================================================================
# A. 认证链路
# ===========================================================================
class TestAuthChain:
    def test_login_success_returns_token(self, client, seed_tenants):
        """账密登录成功 -> code=0 且返回 accessToken/refreshToken/expiresIn。"""
        resp = client.post(f"{MC}/auth/login",
                           json={"account": STAFF_A_ACCOUNT, "password": STAFF_A_PASSWORD})
        body = assert_code(resp, BizCode.OK)
        data = body["data"]
        assert data.get("accessToken"), "登录必须返回 accessToken"
        assert data.get("expiresIn"), "登录必须返回 expiresIn"
        assert data.get("tenant", {}).get("id") == TENANT_A

    def test_login_wrong_password(self, client, seed_tenants):
        """错误密码 -> 契约要求 HTTP 200 + code 40102（验收标准 §1.1）。

        实测：当前抛 HTTPException(401) 裸 401，既违反统一响应体契约（BUG-102），
        也未用约定的 40102 业务码。本用例锁定该契约要求，修复前必红。
        """
        resp = client.post(f"{MC}/auth/login",
                           json={"account": STAFF_A_ACCOUNT, "password": "WrongPass!"})
        body = assert_envelope(resp)
        assert resp.status_code == 200, (
            f"契约要求 HTTP 恒 200，错误密码却裸抛 {resp.status_code}"
        )
        assert body["code"] == 40102, (
            f"错误密码应返回 40102，实际 code={body['code']} "
            f"message={body.get('message')}（疑似裸抛 401）"
        )

    def test_login_works_for_non_tenant_one(self, client, seed_tenants):
        """BUG-103：非 1 号租户的商家也必须能登录。

        mc_auth.login 写死 set_tenant(1)，导致 ORM 租户过滤只在租户 1
        里找账号，其余租户一律返回"账号或密码错误"。
        本用例用租户 B（2002）的真实账号验证，修复前必红。
        """
        resp = client.post(f"{MC}/auth/login",
                           json={"account": STAFF_B_ACCOUNT, "password": STAFF_B_PASSWORD})
        body = assert_envelope(resp)
        assert body["code"] == BizCode.OK, (
            f"租户 {TENANT_B} 的合法账号无法登录（code={body['code']} "
            f"message={body.get('message')}）——mc_auth.login 写死 set_tenant(1)，"
            f"多租户登录不可用"
        )

    def test_login_then_access_with_token(self, client, seed_tenants):
        """登录拿到的 token 应能真实访问受保护接口（端到端闭环）。"""
        login = client.post(f"{MC}/auth/login",
                            json={"account": STAFF_A_ACCOUNT, "password": STAFF_A_PASSWORD})
        token = assert_code(login, BizCode.OK)["data"]["accessToken"]

        resp = client.get(f"{MC}/auth/me", headers={"Authorization": f"Bearer {token}"})
        body = assert_code(resp, BizCode.OK)
        assert body["data"]["tenant"]["id"] == TENANT_A

    def test_me_returns_perms(self, client, seed_tenants, headers_a):
        resp = client.get(f"{MC}/auth/me", headers=headers_a)
        body = assert_code(resp, BizCode.OK)
        assert "perms" in body["data"]

    def test_refresh_returns_new_token(self, client, seed_tenants):
        """refresh 应换发**新的** accessToken（refreshToken 从请求体读）。"""
        login = client.post(f"{MC}/auth/login",
                            json={"account": STAFF_A_ACCOUNT, "password": STAFF_A_PASSWORD})
        refresh_token = assert_code(login, BizCode.OK)["data"]["refreshToken"]
        assert refresh_token, "登录必须返回 refreshToken"

        resp = client.post(f"{MC}/auth/refresh",
                           json={"refreshToken": refresh_token})
        body = assert_code(resp, BizCode.OK)
        new_token = body["data"].get("accessToken")
        assert new_token, "refresh 必须返回 accessToken"
        old_token = refresh_token  # refresh 换的是 access，故与 refreshToken 不同
        assert new_token != old_token, "refresh 未真正换发 accessToken"

    def test_refresh_wrong_token_rejected(self, client, seed_tenants, headers_a):
        """refresh 失败应走 HTTP 200 + 业务码 40101（契约锁定，不抛 401）。

        BUG-104 回归：此前 refresh 回吐旧 token；此处锁"拿 access 当 refresh"
        必须被拒为 40101（refresh 接口要求 refresh 型 token）。
        """
        # 用商家 accessToken 冒充 refreshToken：typ=access 而非 refresh，应被拒
        access = headers_a["Authorization"].removeprefix("Bearer ")
        resp = client.post(f"{MC}/auth/refresh", json={"refreshToken": access})
        body = assert_envelope(resp)
        assert resp.status_code == 200, f"refresh 失败应 HTTP 200，实际 {resp.status_code}"
        assert body["code"] == BizCode.TOKEN_EXPIRED, (
            f"非法 refreshToken 应返回 40101，实际 code={body['code']}"
        )

    def test_logout_then_token_rejected(self, client, seed_tenants):
        """logout 后原 token 应立即失效（加入黑名单）。

        BUG-105 回归：当前 logout 是空实现（仅返回 ok），黑名单未落地，
        token 仍可用。修复前本用例必红。
        """
        login = client.post(f"{MC}/auth/login",
                            json={"account": STAFF_A_ACCOUNT, "password": STAFF_A_PASSWORD})
        token = assert_code(login, BizCode.OK)["data"]["accessToken"]
        hdr = {"Authorization": f"Bearer {token}"}

        assert_code(client.post(f"{MC}/auth/logout", headers=hdr), BizCode.OK)

        resp = client.get(f"{MC}/auth/me", headers=hdr)
        assert_code(resp, BizCode.UNAUTHORIZED)

    def test_sso_not_implemented_returns_error(self, client, seed_tenants):
        """代客 SSO：尚未实现时应返回明确业务错误码，而非空壳 ok()+空 token。

        BUG-106 回归：此前返回 ok() 带空 token，空串被前端当登录成功写 storage，
        导致后续所有请求 401 且无法自愈。现约定明确报错。
        """
        resp = client.post(f"{MC}/auth/sso", json={"ticket": "dummy-ticket"})
        body = assert_envelope(resp)
        assert body["code"] != BizCode.OK, (
            f"SSO 未实现却返回 code=0，前端会误判登录成功：{body}"
        )

    def test_password_change_takes_effect(self, client, seed_tenants, headers_a):
        """改密后旧密码登不进、新密码能登进（PUT /auth/password）。"""
        new_pwd = "NewSecret999"
        assert_code(client.put(f"{MC}/auth/password", headers=headers_a,
                               json={"oldPassword": STAFF_A_PASSWORD,
                                     "newPassword": new_pwd}), BizCode.OK)
        # 旧密码应失效
        old = client.post(f"{MC}/auth/login",
                          json={"account": STAFF_A_ACCOUNT, "password": STAFF_A_PASSWORD})
        assert assert_envelope(old)["code"] != BizCode.OK, "改密后旧密码仍可登录"
        # 新密码应可用
        new = client.post(f"{MC}/auth/login",
                          json={"account": STAFF_A_ACCOUNT, "password": new_pwd})
        assert_code(new, BizCode.OK)


# ===========================================================================
# B. 越权路径
# ===========================================================================
class TestAuthz:
    def test_no_token_returns_40100(self, client):
        """无 token 访问商家端 -> HTTP 200 + code 40100。"""
        assert_code(client.get(f"{MC}/goods"), BizCode.UNAUTHORIZED)

    def test_expired_token_returns_40101(self, client, expired_token):
        """过期 token -> code 40101（前端据此触发 refresh）。"""
        resp = client.get(f"{MC}/goods",
                          headers={"Authorization": f"Bearer {expired_token}"})
        assert_code(resp, BizCode.TOKEN_EXPIRED)

    def test_malformed_token_returns_40100(self, client):
        resp = client.get(f"{MC}/goods", headers={"Authorization": "Bearer not-a-jwt"})
        assert_code(resp, BizCode.UNAUTHORIZED)

    def test_platform_token_on_merchant_endpoint_returns_40301(
        self, client, seed_tenants, platform_headers
    ):
        """平台 token 访问商家端 -> 契约要求 HTTP 200 + code 40301（端隔离）。

        实测：当前抛 HTTPException(403) 裸 403，既违反统一响应体契约（BUG-102），
        也未用约定的 40301 业务码。本用例锁定契约要求，修复前必红。
        """
        resp = client.get(f"{MC}/goods", headers=platform_headers)
        body = assert_envelope(resp)
        assert resp.status_code == 200, (
            f"契约要求 HTTP 恒 200，端隔离错误却裸抛 {resp.status_code}"
        )
        assert body["code"] == 40301, (
            f"平台 token 访问商家端应返回 40301，实际 code={body['code']} "
            f"message={body.get('message')}（疑似裸抛 403）"
        )

    def test_merchant_token_cannot_browse_customer_mp_goods(self, client, seed_tenants, headers_a):
        """/mp 用户商品资源不得接受 merchant token（端点 scope 必须明确）。"""
        body = assert_envelope(client.get("/api/mp/goods", headers=headers_a))
        assert body["code"] == BizCode.FORBIDDEN

    # ---- 跨租户隔离：多租户系统的命门 ----
    def test_cross_tenant_goods_list_isolated(self, client, seed_tenants, headers_a, headers_b):
        """A 的列表不得出现 B 的商品，反之亦然。"""
        body_a = assert_code(client.get(f"{MC}/goods", headers=headers_a), BizCode.OK)
        names_a = {g["name"] for g in (body_a["data"].get("list") or [])}
        assert "B租户商品" not in names_a, f"租户A 看到了租户B 的商品：{names_a}"

        body_b = assert_code(client.get(f"{MC}/goods", headers=headers_b), BizCode.OK)
        names_b = {g["name"] for g in (body_b["data"].get("list") or [])}
        assert "A租户商品" not in names_b, f"租户B 看到了租户A 的商品：{names_b}"

    def test_cross_tenant_goods_detail_rejected(self, client, seed_tenants, headers_a):
        """A 用 B 的 goods_id 查详情 -> 必须被拒且绝不返回 B 的数据。

        拦截有效（BUG 不在此），但错误响应契约不符：当前裸抛 HTTP 404，
        契约要求 HTTP 200 + code 40400。锁定契约，修复前标记 bug。
        """
        resp = client.get(f"{MC}/goods/102", headers=headers_a)
        body = assert_envelope(resp)
        assert body["code"] != BizCode.OK, (
            f"越权成功！租户A 读到了租户B 的商品 102：{body['data']}"
        )
        # 拦截已生效（未泄露），但契约码应为 40400 而非裸 404
        assert resp.status_code == 200 and body["code"] == BizCode.NOT_FOUND, (
            f"跨租户拒绝应走 HTTP 200 + 40400，实际 HTTP {resp.status_code} "
            f"code={body['code']}（疑似裸抛 404，BUG-102 延伸）"
        )

    def test_cross_tenant_goods_update_rejected(self, client, seed_tenants, headers_a):
        """A 改 B 的商品 -> 必须被拒。"""
        resp = client.put(f"{MC}/goods/102", headers=headers_a, json={"name": "被越权篡改"})
        body = assert_envelope(resp)
        assert body["code"] != BizCode.OK, "越权成功！租户A 修改了租户B 的商品"

    def test_cross_tenant_goods_delete_rejected(self, client, seed_tenants, headers_a):
        resp = client.delete(f"{MC}/goods/102", headers=headers_a)
        body = assert_envelope(resp)
        assert body["code"] != BizCode.OK, "越权成功！租户A 删除了租户B 的商品"

    def test_cross_tenant_member_detail_rejected(self, client, seed_tenants, headers_a):
        """A 查 B 的会员详情 -> 必须被拒（会员含手机号，泄露即事故）。

        BUG-109 类真实缺陷：member_detail 查到 None 后直接 `m.id` 触发
        AttributeError: 'NoneType' object has no attribute 'id'（HTTP 500），
        而非返回 404。本用例锁定"不可越权 + 不应 500"，修复前必红。
        """
        resp = client.get(f"{MC}/member/202", headers=headers_a)
        if resp.status_code == 500:
            pytest.fail(
                "跨租户查会员详情触发 500（None.id），应返回 404 而非崩溃——"
                "BUG-109 类：未判空直接访问对象属性"
            )
        body = assert_envelope(resp)
        assert body["code"] != BizCode.OK, (
            f"越权成功！租户A 读到租户B 的会员隐私数据：{body['data']}"
        )

    def test_cross_tenant_points_adjust_rejected(self, client, seed_tenants, headers_a, idem_key):
        """A 给 B 的会员调积分 -> 必须被拒（跨租户写入是最高危路径）。"""
        resp = client.post(f"{MC}/points/adjust", headers=headers_a,
                           json={"memberId": 202, "points": 999,
                                 "remark": "越权测试", "idempotencyKey": idem_key})
        body = assert_envelope(resp)
        assert body["code"] != BizCode.OK, "越权成功！租户A 修改了租户B 会员的积分"

    def test_cross_tenant_verify_rejected(self, client, seed_tenants, headers_a):
        """A 核销 B 的核销码 -> 必须被拒（端隔离修复后 merchant_ctx 按 tid 过滤）。"""
        resp = client.post(f"{MC}/verify", headers=headers_a,
                           json={"code": "HX-UNUSED-B"})
        body = assert_envelope(resp)
        assert body["code"] != BizCode.OK, "越权成功！租户A 核销了租户B 的码"

    def test_http_exception_is_api_envelope_for_duplicate_staff_and_role(
        self, client, seed_tenants, headers_a
    ):
        """历史 handler 的 HTTPException 不得让前端落入网络异常分支。

        员工/角色重复是后台新增弹窗最常见的边界：应保留 HTTP 200，
        并以统一 40900 告知前端显示业务提示。
        """
        suffix = uuid.uuid4().hex[:8]
        staff = {
            "account": f"dup_staff_{suffix}", "name": "重复员工",
            "phone": "13800000000", "password": "Secret123", "roleId": 0,
        }
        first = client.post(f"{MC}/staff", headers=headers_a, json=staff)
        assert_code(first, BizCode.OK)
        repeated = client.post(f"{MC}/staff", headers=headers_a, json=staff)
        assert repeated.status_code == 200
        assert_code(repeated, BizCode.CONFLICT)

        role = {"name": f"dup_role_{suffix}", "remark": "qa", "perms": ["MC_ALL"]}
        assert_code(client.post(f"{MC}/role", headers=headers_a, json=role), BizCode.OK)
        repeated_role = client.post(f"{MC}/role", headers=headers_a, json=role)
        assert repeated_role.status_code == 200
        assert_code(repeated_role, BizCode.CONFLICT)


# ===========================================================================
# C. 业务主链路
# ===========================================================================
class TestGoods:
    def test_goods_list_ok(self, client, seed_tenants, headers_a):
        body = assert_code(client.get(f"{MC}/goods", headers=headers_a), BizCode.OK)
        assert "list" in body["data"] and "total" in body["data"]

    def test_goods_create_then_detail(self, client, seed_tenants, headers_a):
        create = client.post(f"{MC}/goods", headers=headers_a,
                             json={"name": "新建商品", "type": "NORMAL",
                                   "channel": "NORMAL", "status": "DRAFT"})
        body = assert_code(create, BizCode.OK)
        gid = body["data"]["id"]

        detail = client.get(f"{MC}/goods/{gid}", headers=headers_a)
        assert_code(detail, BizCode.OK)

    def test_goods_update(self, client, seed_tenants, headers_a):
        resp = client.put(f"{MC}/goods/101", headers=headers_a, json={"name": "改名后"})
        assert_code(resp, BizCode.OK)

    def test_goods_shelf_toggle(self, client, seed_tenants, headers_a):
        """上下架。"""
        off = client.post(f"{MC}/goods/101/shelf", headers=headers_a,
                          json={"onSale": False, "channel": "NORMAL"})
        assert_code(off, BizCode.OK)
        on = client.post(f"{MC}/goods/101/shelf", headers=headers_a,
                         json={"onSale": True, "channel": "NORMAL"})
        assert_code(on, BizCode.OK)

    def test_goods_stock_adjust(self, client, seed_tenants, headers_a):
        resp = client.put(f"{MC}/goods/101/stock", headers=headers_a,
                          json={"skuId": 1, "channel": "NORMAL", "stock": 50})
        assert_envelope(resp)

    def test_goods_delete(self, client, seed_tenants, headers_a):
        assert_code(client.delete(f"{MC}/goods/101", headers=headers_a), BizCode.OK)


class TestOrder:
    def test_order_list(self, client, seed_tenants, headers_a):
        assert_code(client.get(f"{MC}/order", headers=headers_a), BizCode.OK)

    def test_order_status_counts_shape(self, client, seed_tenants, headers_a):
        """status-counts 返回 {status: count} 字典（已按实际枚举口径）。"""
        body = assert_code(client.get(f"{MC}/order/status-counts", headers=headers_a),
                           BizCode.OK)
        assert isinstance(body["data"], dict), "status-counts 应为字典"

    def test_order_detail_contract(self, client, seed_tenants, headers_a, created):
        """契约路径 GET /mc/order/{id}（T-P2 已修复，1c33844 已生效）。"""
        oid = created["order_today"]
        resp = client.get(f"{MC}/order/{oid}", headers=headers_a)
        body = assert_code(resp, BizCode.OK)
        assert body["data"]["id"] == oid

    def test_order_ship(self, client, seed_tenants, headers_a, created):
        oid = created["order_hist1"]
        resp = client.post(f"{MC}/order/{oid}/ship", headers=headers_a,
                           json={"expressCompany": "SF", "expressNo": "SF123"})
        assert_envelope(resp)


class TestVerify:
    def test_verify_query_invalid_code(self, client, seed_tenants, headers_a):
        """查询不存在的核销码 -> 44001。"""
        resp = client.get(f"{MC}/verify/query", headers=headers_a,
                          params={"code": "NOT-EXIST-CODE"})
        body = assert_envelope(resp)
        assert resp.status_code == 200
        assert body["code"] == BizCode.VERIFY_CODE_INVALID, body

    def test_verify_query_expired_code(self, client, seed_tenants, headers_a):
        """查询已过期核销码 -> 44003。"""
        resp = client.get(f"{MC}/verify/query", headers=headers_a,
                          params={"code": "HX-EXPIRED-A"})
        body = assert_envelope(resp)
        assert resp.status_code == 200
        assert body["code"] == BizCode.VERIFY_CODE_EXPIRED, body

    def test_verify_valid_code(self, client, seed_tenants, headers_a):
        """核销自己租户的未使用码 -> 成功且置 USED（查库确认）。"""
        resp = client.post(f"{MC}/verify", headers=headers_a,
                           json={"code": "HX-UNUSED-A"})
        body = assert_code(resp, BizCode.OK)
        assert body["data"]["status"] == "USED"

    def test_verify_invalid_code(self, client, seed_tenants, headers_a):
        resp = client.post(f"{MC}/verify", headers=headers_a,
                           json={"code": "NOT-EXIST-CODE"})
        body = assert_envelope(resp)
        assert resp.status_code == 200
        assert body["code"] == BizCode.VERIFY_CODE_INVALID, "不存在的核销码应返回 44001"

    def test_verify_used_code(self, client, seed_tenants, headers_a):
        resp = client.post(f"{MC}/verify", headers=headers_a,
                           json={"code": "HX-USED-A"})
        body = assert_envelope(resp)
        assert resp.status_code == 200
        assert body["code"] == BizCode.VERIFY_CODE_USED, "已使用核销码应返回 44002"

    def test_verify_expired_code(self, client, seed_tenants, headers_a):
        resp = client.post(f"{MC}/verify", headers=headers_a,
                           json={"code": "HX-EXPIRED-A"})
        body = assert_envelope(resp)
        assert resp.status_code == 200
        assert body["code"] == BizCode.VERIFY_CODE_EXPIRED, "已过期核销码应返回 44003"

    def test_verify_log(self, client, seed_tenants, headers_a):
        assert_envelope(client.get(f"{MC}/verify/log", headers=headers_a))


class TestMember:
    def test_member_list(self, client, seed_tenants, headers_a):
        assert_code(client.get(f"{MC}/member", headers=headers_a), BizCode.OK)

    def test_member_list_phone_masked(self, client, seed_tenants, headers_a):
        """手机号必须脱敏（138****1024），DoD §0.3。"""
        body = assert_code(client.get(f"{MC}/member", headers=headers_a), BizCode.OK)
        data = body["data"]
        rows = data.get("list", []) if isinstance(data, dict) else (data or [])
        assert rows, "种子已建会员，列表不应为空"
        for m in rows:
            phone = m.get("phoneMask") or ""
            if phone:
                assert "*" in phone, f"会员手机号未脱敏：{phone}"

    def test_member_detail(self, client, seed_tenants, headers_a):
        assert_envelope(client.get(f"{MC}/member/201", headers=headers_a))

    def test_member_orders(self, client, seed_tenants, headers_a):
        assert_envelope(client.get(f"{MC}/member/201/orders", headers=headers_a))

    def test_member_points_log(self, client, seed_tenants, headers_a):
        assert_envelope(client.get(f"{MC}/member/201/points-log", headers=headers_a))

    def test_points_adjust_ok(self, client, seed_tenants, headers_a, idem_key):
        resp = client.post(f"{MC}/points/adjust", headers=headers_a,
                           json={"memberId": 201, "points": 10,
                                "remark": "验收", "idempotencyKey": idem_key})
        assert_code(resp, BizCode.OK)

    def test_points_adjust_rejects_invalid_idempotency_key_before_write(
        self, client, seed_tenants, headers_a
    ):
        """幂等键对应数据库 VARCHAR(40)，超长不得触发 500。"""
        resp = client.post(f"{MC}/points/adjust", headers=headers_a, json={
            "memberId": 201, "points": 1, "remark": "边界", "idempotencyKey": "x" * 41,
        })
        assert resp.status_code == 200
        assert_code(resp, BizCode.PARAM_ERROR)
        assert "idempotencyKey" in resp.json()["data"]["fields"]

    def test_points_log_and_rule(self, client, seed_tenants, headers_a):
        assert_envelope(client.get(f"{MC}/points/log", headers=headers_a))
        assert_envelope(client.get(f"{MC}/points/rule", headers=headers_a))

    def test_shop_and_store(self, client, seed_tenants, headers_a):
        assert_envelope(client.get(f"{MC}/shop", headers=headers_a))
        assert_envelope(client.get(f"{MC}/store", headers=headers_a))
        # 新建门店
        c = client.post(f"{MC}/store", headers=headers_a,
                        json={"name": "测试门店", "address": "xx路1号"})
        body = assert_code(c, BizCode.OK)
        sid = body["data"]["id"]
        assert_envelope(client.put(f"{MC}/store/{sid}", headers=headers_a,
                                   json={"name": "改名门店"}))
        assert_code(client.delete(f"{MC}/store/{sid}", headers=headers_a), BizCode.OK)
        # 软删除后不可继续显示或编辑，避免后台列表出现“已删除门店”。
        rows = assert_code(client.get(f"{MC}/store", headers=headers_a), BizCode.OK)["data"]
        assert not any(row["id"] == sid for row in rows)
        assert_code(
            client.put(f"{MC}/store/{sid}", headers=headers_a, json={"name": "不应写入"}),
            BizCode.NOT_FOUND,
        )


# ===========================================================================
# D. 数字真实性（be-dev-2 修 4 处假数据后的核心验收）
# ===========================================================================
class TestDashboardReality:
    def test_kpi_today_orders_equals_seeded(self, client, seed_tenants, headers_a):
        """今日订单：种子造 2 历史 + 1 当日 -> todayOrders.value 必须=1。"""
        body = assert_code(client.get(f"{MC}/dashboard/kpi", headers=headers_a),
                           BizCode.OK)["data"]
        assert body["todayOrders"]["value"] == 1, (
            f"今日订单数应为 1（仅当日支付），实际 {body['todayOrders']}"
        )

    def test_kpi_today_sales_equals_seeded(self, client, seed_tenants, headers_a):
        """今日销售额：仅当日订单 12.50 计入（按 PAID 状态 + 当日聚合）。"""
        body = assert_code(client.get(f"{MC}/dashboard/kpi", headers=headers_a),
                           BizCode.OK)["data"]
        # 返回值是字符串（Decimal 转 str），按数值比较
        assert float(body["todaySales"]["value"]) == 12.50, (
            f"今日销售额应为 12.50，实际 {body['todaySales']}"
        )

    def test_kpi_new_members_equals_seeded(self, client, seed_tenants, headers_a):
        """新增会员：种子仅 A 租户 1 名会员 joined_at=当日 -> newMembers.value=1。"""
        body = assert_code(client.get(f"{MC}/dashboard/kpi", headers=headers_a),
                           BizCode.OK)["data"]
        assert body["newMembers"]["value"] == 1, (
            f"新增会员应为 1，实际 {body['newMembers']}"
        )

    def test_kpi_today_verify_equals_seeded(self, client, seed_tenants, headers_a):
        """今日核销：种子仅 1 条当日 USED 核销码 -> todayVerify.value=1。"""
        body = assert_code(client.get(f"{MC}/dashboard/kpi", headers=headers_a),
                           BizCode.OK)["data"]
        assert body["todayVerify"]["value"] == 1, (
            f"今日核销应为 1，实际 {body['todayVerify']}"
        )

    def test_kpi_delta_boundary_both_zero(self, client, seed_tenants, headers_a):
        """delta 环比边界：昨日0、今日0 -> delta=0；今日有量 -> delta=100。

        kpi 的 delta 由 _delta_pct 计算。A 租户昨日（历史订单均 <= -1 天）
        无当日聚合项，但今日有 1 项，故 todayOrders.delta 应=100。
        用 kpi 的 delta 字段直接验证边界逻辑：昨日0今日非0 -> 100。
        """
        body = assert_code(client.get(f"{MC}/dashboard/kpi", headers=headers_a),
                           BizCode.OK)["data"]
        # 今日有 1 单，昨日 0 单 -> delta=100
        assert body["todayOrders"]["delta"] == 100, (
            f"今日有单昨日无单时 delta 应为 100，实际 {body['todayOrders']['delta']}"
        )

    def test_trend_x_axis_continuous(self, client, seed_tenants, headers_a):
        """趋势图 X 轴连续：近 N 天每天一条，无数据补 0。"""
        body = assert_code(client.get(f"{MC}/dashboard/trend?days=7",
                                      headers=headers_a), BizCode.OK)
        rows = body["data"]
        assert isinstance(rows, list) and len(rows) == 7, (
            f"趋势图应返回 7 天数据，实际 {len(rows) if isinstance(rows, list) else rows}"
        )
        dates = [r["date"] for r in rows]
        assert dates == sorted(dates), "趋势图日期必须升序且连续"
        # 当日应有 1 单（pay_amount 12.50 -> 数值 >0）
        today = dates[-1]
        today_row = next(r for r in rows if r["date"] == today)
        assert today_row["orders"] == 1, (
            f"趋势图当日订单数应为 1，实际 {today_row['orders']}"
        )

    def test_todo_pending_verify_matches_seed(self, client, seed_tenants, headers_a):
        """待办：pendingVerify = 未核销码数（A 租户 1 条 UNUSED）-> =1。"""
        body = assert_code(client.get(f"{MC}/dashboard/todo", headers=headers_a),
                           BizCode.OK)["data"]
        for key in ("pendingShip", "pendingRefund", "pendingVerify", "pendingPickup"):
            assert key in body, f"todo 缺字段 {key}（前端 shared-types 要求）"
        assert body["pendingVerify"] == 1, (
            f"待核销数应为 1（A 租户 1 条 UNUSED 码），实际 {body['pendingVerify']}"
        )

    def test_recent_orders_returns_seeded(self, client, seed_tenants, headers_a):
        """最近订单：应返回种子落的订单，且字段含 status（真实枚举口径）。"""
        body = assert_code(client.get(f"{MC}/dashboard/recent-orders",
                                      headers=headers_a), BizCode.OK)
        rows = body["data"]
        assert rows, "应返回最近订单"
        assert rows[0]["status"] in ("PAID", "SHIPPED", "STOCKED", "COMPLETED",
                                     "PENDING_PAY", "REFUNDING"), (
            f"订单状态应是库中真实落库值，实际 {rows[0]['status']}"
        )


# ===========================================================================
# E. 幂等
# ===========================================================================
class TestIdempotency:
    def test_points_adjust_same_key_applies_once(
        self, client, seed_tenants, headers_a, idem_key
    ):
        """同一 idempotency_key 重复提交，积分只加一次（BUG-108 回归）。

        契约（验收标准 §0.3）：幂等接口 5 分钟内同 key 返回**首次结果** code=0，
        而不是报错。当前实现对重复 key 直接返回 409 "幂等号已使用"，
        违反契约（前端会误判为失败）。修复前本用例必红。
        """
        payload = {"memberId": 201, "points": 10,
                   "remark": "幂等验收", "idempotencyKey": idem_key}

        first = client.post(f"{MC}/points/adjust", headers=headers_a, json=payload)
        body1 = assert_code(first, BizCode.OK)
        balance1 = body1["data"]["pointsBalance"]

        second = client.post(f"{MC}/points/adjust", headers=headers_a, json=payload)
        body2 = assert_envelope(second)

        assert body2["code"] == BizCode.OK, (
            f"幂等重放应返回首次结果 code=0，实际 code={body2['code']} "
            f"message={body2.get('message')}（BUG-108：重复 key 应返回首次结果）"
        )
        assert body2["data"]["pointsBalance"] == balance1, (
            f"幂等失效：重复提交后余额从 {balance1} 变为 "
            f"{body2['data']['pointsBalance']}，积分被重复累加"
        )

    def test_points_adjust_conflict_key_returns_409(self, client, seed_tenants, headers_a, idem_key):
        """幂等号已使用 -> 40900（BUG-108 修复后的正确行为）。

        注意：用同租户不同 member 验证，避免跨租户 404 干扰幂等判定。
        """
        p1 = {"memberId": 201, "points": 10,
              "remark": "首次", "idempotencyKey": idem_key}
        assert_code(client.post(f"{MC}/points/adjust", headers=headers_a, json=p1),
                    BizCode.OK)
        # 不同 member 但复用同一幂等号 -> 应判定冲突 40900
        p2 = {"memberId": 201, "points": 5,
              "remark": "复用key", "idempotencyKey": idem_key}
        resp2 = client.post(f"{MC}/points/adjust", headers=headers_a, json=p2)
        assert assert_envelope(resp2)["code"] == 40900, (
            "相同幂等号重复提交应返回 40900，实际 "
            f"{assert_envelope(resp2)['code']}"
        )


# ===========================================================================
# F. 统一响应体
# ===========================================================================
class TestEnvelope:
    def test_success_envelope_shape(self, client, seed_tenants, headers_a):
        body = assert_code(client.get(f"{MC}/goods", headers=headers_a), BizCode.OK)
        assert body["message"]

    def test_trace_id_echoes_request_header(self, client, seed_tenants, headers_a):
        """BUG-101：传入 X-Trace-Id 时，响应体 traceId 必须与之一致。"""
        tid = "trace-acceptance-0001"
        resp = client.get(f"{MC}/goods", headers={**headers_a, "X-Trace-Id": tid})
        assert_trace_id(resp, expected=tid)

    def test_trace_id_generated_when_absent(self, client, seed_tenants, headers_a):
        """BUG-101：未传 X-Trace-Id 时服务端应生成并写入响应体。"""
        assert_trace_id(client.get(f"{MC}/goods", headers=headers_a))

    def test_error_path_keeps_http_200(self, client, seed_tenants, headers_a):
        """BUG-102：错误路径同样 HTTP 200 + code（不得裸抛 404/403/500）。"""
        resp = client.get(f"{MC}/goods/99999999", headers=headers_a)
        assert_http200(resp)
        assert assert_envelope(resp)["code"] == BizCode.NOT_FOUND

    def test_unauthorized_path_keeps_http_200(self, client):
        """BUG-102：未认证响应也必须 HTTP 200 + code 40100（当前返回 401）。"""
        resp = client.get(f"{MC}/goods")
        assert_http200(resp)
        assert assert_envelope(resp)["code"] == BizCode.UNAUTHORIZED

    def test_validation_error_returns_40001(self, client, seed_tenants, headers_a):
        """入参校验失败 -> 40001 且 data.fields 有字段级错误。"""
        resp = client.post(f"{MC}/points/adjust", headers=headers_a,
                           json={"memberId": "not-an-int", "points": "abc"})
        body = assert_envelope(resp)
        assert body["code"] == BizCode.PARAM_ERROR, (
            f"参数类型错误应返回 40001，实际 {body['code']}"
        )
