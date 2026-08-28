"""手机号加密存储回归：字段层透明加解密 + 兼容存量明文。"""
from __future__ import annotations

from app.core.crypto_phone import decrypt_phone, encrypt_phone


def test_encrypt_decrypt_roundtrip():
    plain = "13800001234"
    cipher = encrypt_phone(plain)
    assert cipher != plain and cipher.startswith("enc:")
    assert decrypt_phone(cipher) == plain


def test_decrypt_tolerates_legacy_plaintext():
    assert decrypt_phone("13800001234") == "13800001234"
    assert decrypt_phone("") == ""
    assert decrypt_phone(None) == ""


def test_encrypted_string_type_transparent():
    """EncryptedString 写入加密、读出解密，对应用层透明。"""
    from app.db.types import EncryptedString

    col = EncryptedString(255)
    plain = "13700008888"
    bound = col.process_bind_param(plain, None)
    assert bound.startswith("enc:") and bound != plain
    assert col.process_result_value(bound, None) == plain
    # 结果值为 None 时保持 None
    assert col.process_result_value(None, None) is None
