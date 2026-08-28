"""微信开放能力适配器。

这里仅负责把小程序 ``code`` 换成稳定身份标识；租户路由、会员建档和
Token 签发都留在业务层。任何路径都不得记录 app secret 或完整响应。
"""
from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings


class WechatCode2SessionError(Exception):
    """微信拒绝或无法完成 code2session 时抛出。"""


def code2session(*, appid: str, secret: str, code: str) -> dict[str, str]:
    """调用微信 code2session；本地 fake 必须由显式开关和 fake: code 同时开启。"""
    normalized_code = code.strip()
    if settings.WECHAT_LOGIN_FAKE_ENABLED:
        if normalized_code.startswith("fake:") and len(normalized_code) > len("fake:"):
            return {"openid": f"fake-openid-{normalized_code[len('fake:'):]}", "unionid": ""}
        raise WechatCode2SessionError("本地 fake 登录仅接受 fake: 开头的 code")

    if not appid or not secret or not normalized_code:
        raise WechatCode2SessionError("微信登录配置或 code 无效")
    try:
        response = httpx.get(
            settings.WECHAT_CODE2SESSION_URL,
            params={
                "appid": appid,
                "secret": secret,
                "js_code": normalized_code,
                "grant_type": "authorization_code",
            },
            timeout=settings.WECHAT_CODE2SESSION_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data: Any = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WechatCode2SessionError("微信登录服务暂不可用") from exc

    openid = data.get("openid") if isinstance(data, dict) else None
    if not isinstance(openid, str) or not openid:
        # 故意不返回 errcode/errmsg，它们可能携带上游调试信息；客户端只需知道 code 无效。
        raise WechatCode2SessionError("微信 code 无效或已过期")
    unionid = data.get("unionid") if isinstance(data, dict) else ""
    return {"openid": openid, "unionid": unionid if isinstance(unionid, str) else ""}
