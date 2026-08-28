"""Token 版本吊销核心机制单测（Redis 由 conftest 替换为 fakeredis）。"""
from app.core.redis import bump_token_version, get_token_version
from app.core.security import (
    create_access_token,
    decode_token,
    token_version_valid,
)


def test_token_version_embed_and_revoke():
    scope, sub = "merchant", "999"
    assert get_token_version(scope, sub) == 0

    tok1 = create_access_token(subject=sub, scope=scope, tenant_id=1)
    p1 = decode_token(tok1)
    assert p1.get("tv") == 0
    assert token_version_valid(p1) is True

    # 密码变更：版本自增
    assert bump_token_version(scope, sub) == 1
    assert get_token_version(scope, sub) == 1

    # 旧 token（tv=0）立即失效
    assert token_version_valid(p1) is False

    # 新 token 嵌入新版本且有效
    tok2 = create_access_token(subject=sub, scope=scope, tenant_id=1)
    p2 = decode_token(tok2)
    assert p2.get("tv") == 1
    assert token_version_valid(p2) is True


def test_token_without_tv_claim_is_allowed():
    """上线前签发的无 tv 旧 token 不应被版本校验拒绝。"""
    tok = create_access_token(subject="x", scope="platform")
    p = decode_token(tok)
    p.pop("tv", None)
    assert token_version_valid(p) is True
