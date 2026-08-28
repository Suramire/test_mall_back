# 用户小程序微信登录配置

用户端 `POST /api/c/auth/wx-login` 接受 `appid`（也兼容 `appId`）和一次性 `code`。
服务端先按 `pf_tenant.wx_appid` 反查租户，再使用该租户保存的 Secret 调微信
`code2session`；客户端传入的 `tenantId`、`openid` 一律不参与授权。

## 配置

```dotenv
WECHAT_LOGIN_FAKE_ENABLED=false
WECHAT_CODE2SESSION_URL=https://api.weixin.qq.com/sns/jscode2session
WECHAT_CODE2SESSION_TIMEOUT_SECONDS=5
WECHAT_SECRET_ENCRYPT_KEY=<独立随机密钥>
```

租户在平台后台配置的 `wxAppid` / `wxSecret` 对应**用户小程序**的 AppID/Secret，
写库前采用 AES-256-GCM 变为 `sec:` 密文；读取接口不回显。Secret 不会写入日志、
接口响应或文档示例。生产环境必须保持
`WECHAT_LOGIN_FAKE_ENABLED=false`，并为每个租户配置真实凭据。

本地契约测试可显式把 `WECHAT_LOGIN_FAKE_ENABLED=true`，此时只允许形如
`fake:<稳定测试标识>` 的 code；任意普通 code 仍会被拒绝。该开关不能用于
商家小程序或企微登录。

商家小程序使用平台统一 AppID，当前仅支持已有的账号密码登录兜底；企微扫码
登录需要企业应用、回调域名及 `getuserinfo` 资质，未具备这些外部条件前不得
伪造 `wx.qy.login` 成功结果。

## 迁移步骤

1. 在部署环境的 Secret 管理器中设置独立的 `WECHAT_SECRET_ENCRYPT_KEY`，并重启后端；不要使用 `.env` 提交真实值。
2. 备份 `pf_tenant` 后执行 `cd backend && .venv/bin/alembic upgrade head`。迁移 `0009_encrypt_tenant_wx_secret` 只转换非空且不以 `sec:` 开头的历史值。
3. 用一个租户 AppID 和真实微信 `code` 冒烟验证；迁移过程和应用日志均不会输出 Secret。若未配置微信外部资质，只验证 `WECHAT_LOGIN_FAKE_ENABLED=true` 下的 `fake:<id>` 契约，结束后立即关回 `false`。
