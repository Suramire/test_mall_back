"""JWT 生成/校验 + 密码哈希(BCrypt)。统一 Payload 结构。

Payload 含 scope 字段区分 platform/merchant/customer，按端统一 JWT。
"""
from __future__ import annotations

import base64
import hashlib
import time
from datetime import UTC, datetime
from typing import Any

import bcrypt
import jwt

from app.core.config import settings
from app.core.exceptions import TokenExpiredError, UnauthorizedError
from app.core.redis import get_token_version

# passlib 1.7.4 无法识别 bcrypt 5.x(__about__ 被移除)，会在 set_backend 阶段抛
# "password cannot be longer than 72 bytes"，故直接基于 bcrypt 库实现。
# bcrypt 的 72 字节上限由 _to_bcrypt_secret 的 sha256 预摘要消化。
_BCRYPT_ROUNDS = settings.BCRYPT_ROUNDS

# JWT scope
SCOPE_PLATFORM = "platform"
SCOPE_MERCHANT = "merchant"
SCOPE_CUSTOMER = "customer"


def _to_bcrypt_secret(plain: str) -> bytes:
    """口令转 bcrypt 输入：先 sha256 摘要再 base64，恒定 44 字节。

    不采用截断（无论按字符还是按字节），原因：
    1. 凭证碰撞：截断后 'A'*73 与 'A'*72 产生同一 secret，任何超长口令的
       有效强度被压到前 72 字节，攻击者只需碰撞前缀即可登录。
    2. 按字节截断可能把多字节字符切成半个，产生非法 UTF-8 尾巴。
    sha256 摘要固定 32 字节、base64 后 44 字节，稳定落在 72 字节限制内，
    且全量明文参与运算，无长度上限、无前缀碰撞。
    """
    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(plain: str) -> str:
    """BCrypt 哈希（sha256 预摘要），兼容 bcrypt 5.x，无口令长度上限。"""
    secret = _to_bcrypt_secret(plain)
    hashed = bcrypt.hashpw(secret, bcrypt.gensalt(_BCRYPT_ROUNDS))
    return hashed.decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """校验口令，异常（坏哈希/编码错误）一律视为不匹配。"""
    try:
        return bcrypt.checkpw(_to_bcrypt_secret(plain), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def _create_token(payload: dict, expires_in: int) -> str:
    now = int(time.time())
    body = {
        "iat": now,
        "exp": now + expires_in,
        **payload,
    }
    return jwt.encode(body, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(
    subject: str,
    scope: str,
    tenant_id: int | None = None,
    perms: list[str] | None = None,
    features: list[str] | None = None,
    impersonating: bool = False,
    extra: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "sub": subject,  # 员工ID 或 会员ID
        "scope": scope,
        "jti": _new_jti(),
        "typ": "access",  # 与 refresh 区分，防止 accessToken 被当作刷新令牌重放
    }
    if tenant_id is not None:
        payload["tid"] = tenant_id
    if perms:
        payload["perms"] = perms
    if features:
        payload["features"] = features
    if impersonating:
        payload["imp"] = True
    if extra:
        payload.update(extra)
    _embed_token_version(payload, subject, scope)
    return _create_token(payload, settings.JWT_ACCESS_EXPIRE)


def create_refresh_token(subject: str, scope: str, tenant_id: int | None = None) -> str:
    """签发刷新令牌。

    tenant_id 对商家/会员端必填：刷新时不再查库确定租户，直接沿用令牌内的 tid，
    否则 refresh 出来的 accessToken 会丢失租户上下文（ORM 钩子拿不到 tid）。
    平台端无租户，保持 None。
    """
    payload: dict[str, Any] = {"sub": subject, "scope": scope, "jti": _new_jti(), "typ": "refresh"}
    if tenant_id is not None:
        payload["tid"] = tenant_id
    _embed_token_version(payload, subject, scope)
    return _create_token(payload, settings.JWT_REFRESH_EXPIRE)


def _new_jti() -> str:
    import uuid

    return uuid.uuid4().hex


def _embed_token_version(payload: dict[str, Any], subject: str | None, scope: str | None) -> None:
    """在 payload 写入当前 token 版本，用于密码重置后吊销旧 token。

    subject/scope 缺失（非用户绑定令牌）或 redis 不可用时跳过写入，
    调用方对无 tv 声明的旧 token 不做版本校验。
    """
    if subject is None or scope is None:
        return
    tv = get_token_version(scope, str(subject))
    if tv is not None:
        payload["tv"] = tv


def token_version_valid(payload: dict[str, Any]) -> bool:
    """校验 token 的 tv 是否与当前版本一致。

    - 无 tv 声明（本功能上线前签发的旧 token）：放行；
    - redis 不可用（get_token_version 返回 None）：降级放行；
    - 版本不一致（密码已变更）：拒绝。
    """
    tv = payload.get("tv")
    if tv is None:
        return True
    sub = payload.get("sub")
    scope = payload.get("scope")
    if sub is None or scope is None:
        return True
    cur = get_token_version(scope, str(sub))
    if cur is None:
        return True
    return tv == cur


def decode_token(token: str, verify_exp: bool = True) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": verify_exp},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError() from exc
    except jwt.PyJWTError as exc:
        raise UnauthorizedError() from exc


def is_refresh_token(payload: dict) -> bool:
    return payload.get("typ") == "refresh"


def utc_now() -> datetime:
    return datetime.now(UTC)
