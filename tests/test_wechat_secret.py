from app.core.crypto_secret import decrypt_secret, encrypt_secret


def test_wechat_secret_is_encrypted_at_rest_and_round_trips():
    plain = "tenant-secret-not-for-log"
    encrypted = encrypt_secret(plain)
    assert encrypted.startswith("sec:")
    assert plain not in encrypted
    assert decrypt_secret(encrypted) == plain
    assert decrypt_secret(plain) == plain  # migration 前存量兼容
