"""将 mb_member.phone_enc 存量明文统一加密为 AES-GCM 密文。

引入 app/db/types.EncryptedString 后，字段层写入即透明加密，但迁移前已落库的
明文行不会自动改写。本迁移对无 ``enc:`` 前缀的存量行批量加密，使其与新写入一致；
解密分支（decrypt_phone 对无前缀值按明文返回）作为兼容兜底保留。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.crypto_phone import ENC_PREFIX, decrypt_phone, encrypt_phone

revision: str = "0006_phone_enc_encrypt"
down_revision: str | None = "0005_staff_wecom_userid"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _reencrypt_rows(encrypt: bool) -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, phone_enc FROM mb_member")).fetchall()
    for rid, value in rows:
        if not value:
            continue
        if encrypt:
            if value.startswith(ENC_PREFIX):
                continue
            plain = decrypt_phone(value)
            new_value = encrypt_phone(plain)
        else:
            if not value.startswith(ENC_PREFIX):
                continue
            new_value = decrypt_phone(value)
        bind.execute(
            sa.text("UPDATE mb_member SET phone_enc=:v WHERE id=:i"),
            {"v": new_value, "i": rid},
        )


def upgrade() -> None:
    _reencrypt_rows(encrypt=True)


def downgrade() -> None:
    _reencrypt_rows(encrypt=False)
