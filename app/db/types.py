"""透明加密列类型：写入加密、读出解密。"""
from __future__ import annotations

from typing import Any

from sqlalchemy.types import String, TypeDecorator

from app.core.crypto_phone import decrypt_phone, encrypt_phone


class EncryptedString(TypeDecorator):
    """对字符串列透明做 AES-GCM 加密存储，应用层始终看到明文。"""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return encrypt_phone(value)

    def process_result_value(self, value: str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return decrypt_phone(value)
