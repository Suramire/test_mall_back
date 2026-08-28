"""商家端改密后旧 token 吊销的端到端回归。

验证：改密前旧 access 可访问；改密后旧 access 与旧 refresh 均被拒绝；
并以新密码重新登录改回原密码，避免污染其他用例。
"""
STAFF_ACCOUNT = "shopA"
OLD_PWD = "Secret123"
NEW_PWD = "NewPass123"


def test_merchant_password_change_revokes_old_tokens(client, seed_tenants):
    login = client.post("/api/mc/auth/login", json={"account": STAFF_ACCOUNT, "password": OLD_PWD})
    assert login.json()["code"] == 0
    body = login.json()["data"]
    old_access = body["accessToken"]
    old_refresh = body["refreshToken"]
    h = {"Authorization": f"Bearer {old_access}"}

    # 改密前：访问正常
    assert client.get("/api/mc/auth/me", headers=h).json()["code"] == 0

    # 改密（携带旧 token，请求当下仍有效，handler 内自增版本）
    r = client.put(
        "/api/mc/auth/password",
        headers=h,
        json={"oldPassword": OLD_PWD, "newPassword": NEW_PWD},
    )
    assert r.json()["code"] == 0

    # 改密后：旧 access 立即失效
    assert client.get("/api/mc/auth/me", headers=h).json()["code"] == 40100
    # 改密后：旧 refresh 立即失效
    assert (
        client.post("/api/mc/auth/refresh", json={"refreshToken": old_refresh}).json()["code"]
        == 40101
    )

    # 还原密码，避免影响其他用例（用新密码重新登录拿合法 token 再改回）
    new_login = client.post("/api/mc/auth/login", json={"account": STAFF_ACCOUNT, "password": NEW_PWD})
    assert new_login.json()["code"] == 0
    new_access = new_login.json()["data"]["accessToken"]
    rev = client.put(
        "/api/mc/auth/password",
        headers={"Authorization": f"Bearer {new_access}"},
        json={"oldPassword": NEW_PWD, "newPassword": OLD_PWD},
    )
    assert rev.json()["code"] == 0
