"""手机号加密存储工具。

设计要点：
- 算法：AES-256-GCM（带认证标签，防篡改）。
- 密钥：由 ``PHONE_ENCRYPT_KEY`` 派生（缺省回退到 ``JWT_SECRET``），
  经 SHA-256 得到 32 字节密钥；生产环境应通过环境变量设置独立密钥。
- 存储格式：``enc:<base64(nonce|tag|ciphertext)>``，前缀用于区分迁移前明文存量。
- 解密时对无前缀值按明文返回，兼容存量数据，待迁移脚本统一加密后该分支不再命中。
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

_PREFIX = "enc:"
ENC_PREFIX = _PREFIX
_NONCE_LEN = 12


def _derive_key() -> bytes:
    raw = (settings.PHONE_ENCRYPT_KEY or settings.JWT_SECRET).encode("utf-8")
    return hashlib.sha256(raw).digest()


def encrypt_phone(plain: str | None) -> str:
    """加密手机号；空值原样返回。"""
    if not plain:
        return ""
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(_derive_key()).encrypt(nonce, plain.encode("utf-8"), None)
    return _PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt_phone(value: str | None) -> str:
    """解密手机号；空值或无前缀（存量明文）原样返回以兼容迁移前数据。"""
    if not value:
        return ""
    if not value.startswith(_PREFIX):
        return value
    raw = base64.b64decode(value[len(_PREFIX):])
    nonce, ct = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
    return AESGCM(_derive_key()).decrypt(nonce, ct, None).decode("utf-8")
