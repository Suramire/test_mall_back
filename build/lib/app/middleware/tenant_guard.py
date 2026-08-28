"""TenantGuard 中间件（多租户鉴权入口）。

职责：
- 解析 Authorization: Bearer <JWT>，校验签名/过期。
- 平台端 (/api/pf) 不设置 tenant（pf_* 为平台级表，无 tenant_id）。
- 其余三端从 JWT 解析 tenant_id → set 到 TenantContext（ContextVar）。
- 缺失 tenant 且非平台端 → Fail-Fast（40100）。
- 请求结束 reset 上下文，避免串租户。

platform scope 的 JWT 不含 tid；merchant/customer scope 含 tid。
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.errors import BizCode
from app.core.redis import is_token_blacklisted
from app.core.security import (
    SCOPE_CUSTOMER,
    SCOPE_MERCHANT,
    SCOPE_PLATFORM,
    decode_token,
    token_version_valid,
)
from app.core.tenant_context import reset, set_staff, set_tenant

# 平台端前缀：不注入 tenant 上下文
_PLATFORM_PREFIX = "/api/pf"

# 前缀 → 允许的 scope 强制映射（框架级兜底，业务层 merchant_ctx() 等校验保留为纵深防御）。
# 关于 /api/mp 的裁定依据（team-lead 终裁）：
#   1. 前端所有 /mp/* 调用方 100% 落在 mp-user 包（用户端），mp-merchant 目前是空壳，没有任何 /mp/* 引用。
#   2. 后端不存在签发 mp 端 scope Token 的登录入口（mp scope 的 issuer 缺失）。
#   3. 综合 1+2：/mp 在现有实现里等价用户端，按 SCOPE_CUSTOMER 强制，可闭合
#      "用户端 Token 误打误撞命中商家小程序写接口" 的越权面，且无任何既有调用会被误锁。
_SCOPE_BY_PREFIX: tuple[tuple[str, str | tuple[str, ...]], ...] = (
    ("/api/pf/", SCOPE_PLATFORM),
    ("/api/mc/", SCOPE_MERCHANT),
    # /mp 同时承载用户小程序与商家小程序。具体资源再由路由层按
    # customer_ctx()/merchant_ctx() 守卫，不能在前缀层把商家 token 一刀切拒绝。
    ("/api/mp/", (SCOPE_CUSTOMER, SCOPE_MERCHANT)),
    ("/api/c/", SCOPE_CUSTOMER),
)

# 无需携带 Token 的公开路径（登录态尚未建立）。
# 必须精确豁免，否则登录接口自身会被锁死导致整个系统无法登录。
_PUBLIC_PATHS = frozenset(
    {
        "/api/mc/auth/login",
        "/api/mc/auth/sso",
        "/api/mp/auth/login",
        "/api/mp/auth/merchant-login",
        "/api/c/auth/login",
        "/api/c/auth/wx-login",
        "/api/pf/auth/login",
        "/api/common/health",
        "/api/openapi.json",
        "/api/docs",
    }
)

# 续期路径：必须容忍**过期**的 access token —— 这正是 refresh 存在的理由。
# 前端带着已过期的 token 来换新 token（web-kit http.ts 会附上 Authorization 头），
# 若在此处按常规校验过期，会 401/40101 → 前端判定需重登 → 一过期就掉登录，
# refresh 机制 100% 失效。因此这些路径解码时跳过 exp 校验。
#
# 注意：跳过的**仅是过期校验**。签名/算法照常校验、jti 黑名单照常检查、
# 端前缀 scope 照常强制（防止平台端 Token 去换商家端 Token），
# 三道防线都在，不要因为"它在豁免名单里"就以为整条链路不设防。
_REFRESH_PATHS = frozenset(
    {
        "/api/mc/auth/refresh",
        "/api/pf/auth/refresh",
    }
)


_SCOPE_LABELS = {
    SCOPE_PLATFORM: "平台端",
    SCOPE_MERCHANT: "商家端",
    SCOPE_CUSTOMER: "用户端",
}


def _required_scope(path: str) -> str | None:
    """返回该路径要求的 scope；无强制要求时返回 None。"""
    if path in _PUBLIC_PATHS:
        return None
    for prefix, scope in _SCOPE_BY_PREFIX:
        if path.startswith(prefix):
            return scope
    return None


class TenantGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        is_platform = path.startswith(_PLATFORM_PREFIX)

        auth = request.headers.get("Authorization", "")
        token = ""
        if auth.startswith("Bearer "):
            token = auth[7:].strip()

        if token:
            # 续期端点容忍过期 token（签名仍然校验）；其余路径按常规校验过期。
            try:
                payload = decode_token(token, verify_exp=path not in _REFRESH_PATHS)
            except Exception as exc:  # TokenExpiredError / UnauthorizedError
                return self._err(BizCode.TOKEN_EXPIRED if "过期" in str(exc) else BizCode.UNAUTHORIZED, str(exc))

            jti = payload.get("jti")
            if jti and is_token_blacklisted(jti):
                return self._err(BizCode.UNAUTHORIZED, "Token 已失效，请重新登录")

            # 密码重置后旧 token 吊销：token 版本不一致立即失效。
            if not token_version_valid(payload):
                return self._err(BizCode.UNAUTHORIZED, "登录态已失效，请重新登录")

            scope = payload.get("scope")
            tenant_id = payload.get("tid")

            # 全程保存 payload 供依赖层读取 perms/features
            request.state.auth = payload

            # 框架级 scope 兜底：端前缀与 Token scope 不匹配一律拒绝，
            # 不依赖各 handler 自行调用 merchant_ctx() 之类的人工校验。
            required = _required_scope(path)
            accepted = (required,) if isinstance(required, str) else required
            if accepted is not None and scope not in accepted:
                label = "/".join(_SCOPE_LABELS.get(x, x) for x in accepted)
                return self._forbidden(f"仅{label}可访问")

            if scope == SCOPE_PLATFORM:
                # 平台端：不设置 tenant
                pass
            else:
                if tenant_id is None:
                    return self._err(BizCode.UNAUTHORIZED, "Token 缺少租户上下文(tid)")
                set_tenant(int(tenant_id))
                # 停用/过期租户立即使已有 Token 失效（启用后可重新登录）
                try:
                    from app.db.session import SessionLocal
                    from app.models.pf_tenant import PfTenant
                    with SessionLocal() as _s:
                        _t = _s.get(PfTenant, int(tenant_id))
                        if _t is not None and _t.status in ("DISABLED", "EXPIRED"):
                            return self._err(BizCode.UNAUTHORIZED, "租户已停用或到期")
                        # 商家员工禁用后，已有 access token 也必须立即失效。
                        if scope == SCOPE_MERCHANT and (sub := payload.get("sub")):
                            from app.models.mc_staff import McStaff
                            _staff = _s.get(McStaff, int(sub))
                            # 部分内部任务/兼容调用使用已签发的租户级 token（无员工行）；
                            # 存在员工记录时才执行即时状态吊销。
                            if _staff is not None and _staff.status != "ENABLED":
                                return self._err(BizCode.UNAUTHORIZED, "员工账号已停用")
                            # 在读取请求体/执行 handler 前完成关键 mutation 权限判断。
                            if _staff is not None and not _staff.is_admin and "MC_ALL" not in (payload.get("perms") or []):
                                path = request.url.path
                                required = None
                                if "/goods" in path and request.method == "POST": required = "GOODS_CREATE"
                                elif "/goods" in path and request.method == "PUT": required = "GOODS_EDIT"
                                elif "/goods" in path and request.method == "DELETE": required = "GOODS_DELETE"
                                elif "/staff" in path: required = "SET_USER"
                                elif "/role" in path: required = "SET_ROLE"
                                elif "/points/adjust" in path: required = "POINTS_ADJUST"
                                elif "/verify" in path and request.method == "POST": required = "VERIFY_DO"
                                elif "/store" in path: required = "SET_STORE"
                                elif "/order/" in path and (path.endswith("/ship") or path.endswith("/batch-ship")): required = "ORDER_SHIP"
                                elif "/refund/" in path and path.endswith(("/approve", "/reject", "/audit")): required = "ORDER_REFUND_AUDIT"
                                elif "/msg-config" in path or "/message/" in path: required = "SET_MESSAGE"
                                elif path.endswith("/shop"): required = "SET_SHOP"
                                elif "/freight-template" in path: required = "FREIGHT_MANAGE"
                                elif "/member/level" in path or "/level/" in path: required = "MEMBER_LEVEL"
                                elif "/points/rule" in path: required = "POINTS_RULE"
                                elif "/points/import" in path: required = "POINTS_IMPORT"
                                if required:
                                    from app.models.mc_config import McRole
                                    _role = _s.get(McRole, _staff.role_id) if _staff.role_id else None
                                    if not _role or required not in (_role.perms or []):
                                        return self._forbidden("无权限执行此操作")
                except Exception:
                    pass

            # 绑定 staff/员工上下文（sub=员工ID或会员ID）
            sub = payload.get("sub")
            if sub is not None:
                try:
                    set_staff(int(sub), name=None)
                except (TypeError, ValueError):
                    pass
        elif (path.startswith('/api/mc') or path.startswith('/api/mp') or (path.startswith('/api/c/') and not path.startswith('/api/common'))) and path not in ('/api/mc/auth/login','/api/mc/auth/refresh','/api/mc/auth/sso','/api/mp/auth/login','/api/mp/auth/merchant-login','/api/c/auth/login','/api/c/auth/wx-login'):
            return self._err(BizCode.UNAUTHORIZED, "未登录或 Token 无效")

        try:
            response = await call_next(request)
        finally:
            reset()
        return response

    @staticmethod
    def _err(code: int, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=200,
            content={"code": code, "message": message, "data": None, "traceId": ""},
        )

    @staticmethod
    def _forbidden(message: str) -> JSONResponse:
        return JSONResponse(
            status_code=200,
            content={"code": BizCode.FORBIDDEN, "message": message, "data": None, "traceId": ""},
        )
