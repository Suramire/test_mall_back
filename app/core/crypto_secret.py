"""敏感第三方凭据的字段级 AES-256-GCM 加密。"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

_PREFIX = "sec:"
_NONCE_LEN = 12


def _key() -> bytes:
    return hashlib.sha256((settings.WECHAT_SECRET_ENCRYPT_KEY or settings.JWT_SECRET).encode()).digest()


def encrypt_secret(value: str | None) -> str:
    if not value:
        return ""
    nonce = os.urandom(_NONCE_LEN)
    return _PREFIX + base64.b64encode(nonce + AESGCM(_key()).encrypt(nonce, value.encode(), None)).decode()


def decrypt_secret(value: str | None) -> str:
    if not value or not value.startswith(_PREFIX):
        return value or ""  # 兼容历史 TODO 阶段明文，写入后会转为 sec:。
    raw = base64.b64decode(value[len(_PREFIX):])
    return AESGCM(_key()).decrypt(raw[:_NONCE_LEN], raw[_NONCE_LEN:], None).decode()
