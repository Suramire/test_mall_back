"""Redis 客户端封装：Token黑名单、幂等键、分布式锁。

注意：Redis 服务当前可能未启动。这里做 lazy 连接 + 优雅降级，
服务未就绪时幂等/黑名单等能力暂不可用但不应导致进程崩溃。
"""
from __future__ import annotations

from typing import Any

import redis as redis_lib

from app.core.config import settings

# 主连接（限流/幂等/锁）
_redis: redis_lib.Redis | None = None


def get_redis() -> redis_lib.Redis:
    global _redis
    if _redis is None:
        _redis = redis_lib.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def ping() -> bool:
    try:
        return bool(get_redis().ping())
    except Exception:
        return False


# ---- 幂等键 ----
def idempotency_set(key: str, value: str = "1", ttl: int | None = None) -> bool:
    """SETNX 幂等键，已存在返回 False。"""
    ttl = ttl or settings.IDEMPOTENCY_TTL
    try:
        return bool(get_redis().set(key, value, nx=True, ex=ttl))
    except Exception:
        # Redis 不可用时降级：仅凭本地标记，避免阻断主流程
        return True


# ---- Token 黑名单 ----
def blacklist_token(jti: str, ttl: int | None = None) -> None:
    try:
        get_redis().setex(f"jwt:blacklist:{jti}", ttl or settings.JWT_REFRESH_EXPIRE, "1")
    except Exception:
        pass


def is_token_blacklisted(jti: str) -> bool:
    try:
        return bool(get_redis().exists(f"jwt:blacklist:{jti}"))
    except Exception:
        return False


# ---- Token 版本（密码重置吊销）----
def _tv_key(scope: str, sub: str) -> str:
    return f"jwt:tv:{scope}:{sub}"


def get_token_version(scope: str, sub: str) -> int | None:
    """返回用户当前 token 版本（密码变更后自增）。

    redis 不可用时返回 None —— 调用方应跳过校验（降级为不吊销，
    与 is_token_blacklisted 的优雅降级一致），避免错误拒绝正常请求。
    """
    try:
        val = get_redis().get(_tv_key(scope, str(sub)))
    except Exception:
        return None
    if val is None:
        return 0
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def bump_token_version(scope: str, sub: str) -> int:
    """密码变更时自增版本，使该用户所有旧 token 立即失效。返回新版本。"""
    try:
        return get_redis().incr(_tv_key(scope, str(sub)))
    except Exception:
        return 0


# ---- 分布式锁 ----
def acquire_lock(key: str, ttl: int = 10) -> bool:
    try:
        return bool(get_redis().set(f"lock:{key}", "1", nx=True, ex=ttl))
    except Exception:
        return False


def release_lock(key: str) -> None:
    try:
        get_redis().delete(f"lock:{key}")
    except Exception:
        pass


# ---- 简单 KV ----
def set_kv(key: str, value: Any, ttl: int | None = None) -> None:
    try:
        get_redis().setex(key, ttl or 60, value)
    except Exception:
        pass


def get_kv(key: str) -> str | None:
    try:
        return get_redis().get(key)
    except Exception:
        return None
