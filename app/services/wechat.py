"""微信小程序服务端接口。

只依赖 httpx，不引第三方 SDK。登录链路上的外部调用必须**短超时 + 明确报错**，
否则微信接口一慢会直接拖垮 /auth/wechat/login。

本模块刻意只保留登录必需的能力：
  - code2session：wx.login 的 code 换 openid / unionid / session_key
  - placeholder_email / derive_password：把 openid 映射成 GoTrue 账号所需材料

没有实现 getuserphonenumber：它要求企业主体 + 认证 + 单独付费，个人主体用不了，
且本项目不做手机号绑定，引进来只是死代码。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Dict, Optional

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthError, ConfigError, ValidationError

logger = logging.getLogger("app.wechat")

CODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"

# 微信错误码 → 用户能看懂的中文。没覆盖到的用 errmsg 兜底。
_ERR_TEXT: Dict[int, str] = {
    40029: "登录凭证无效，请重试",
    45011: "操作过于频繁，请稍后再试",
    40226: "账号已被限制登录",
    -1: "微信服务暂时不可用，请稍后重试",
}


class WeChatService:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return bool(self._settings.wechat_appid and self._settings.wechat_app_secret)

    def _require(self) -> None:
        if not self.enabled:
            raise ConfigError("未配置 WECHAT_APPID / WECHAT_APP_SECRET，微信登录不可用")

    # ---------------- openid → GoTrue 账号材料 ----------------
    def derive_password(self, openid: str) -> str:
        """从 openid 派生可复现的登录密码（不落库，现算现用）。

        微信用户没有密码，但 GoTrue 的 password grant 必须要密码。这里用
        HMAC 从 openid 派生一个确定性密码：每次登录都能现算出来，改天换台
        机器也不影响。30 位、含大小写与符号，满足 Supabase 密码强度要求。
        """
        key = self._settings._wechat_link_key
        if not key:
            raise ConfigError("未配置 WECHAT_LINK_SECRET 或 SUPABASE_JWT_SECRET，无法派生微信账号密码")
        digest = hmac.new(key.encode("utf-8"), openid.encode("utf-8"), hashlib.sha256).hexdigest()
        return "Jbs!wx" + digest[:24]

    @staticmethod
    def placeholder_email(openid: str) -> str:
        """微信用户的占位邮箱。

        用哈希而非 openid 原值：access token 的 claims 会带上 email，
        直接放 openid 等于把用户唯一标识泄露给所有下游。
        """
        tail = hashlib.sha256(openid.encode("utf-8")).hexdigest()[:20]
        return f"wx_{tail}@wechat.local"

    # ---------------- 登录 ----------------
    async def code2session(self, code: str) -> Dict[str, Any]:
        """用 wx.login 的 code 换 openid / unionid / session_key。

        注意：code 一次性、5 分钟有效。并发拿同一个 code 兑换会有一个失败，
        前端不要把同一个 code 重试整个登录流程，应重新 Taro.login()。
        """
        self._require()
        params = {
            "appid": self._settings.wechat_appid,
            "secret": self._settings.wechat_app_secret,
            "js_code": code,
            "grant_type": "authorization_code",
        }
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(CODE2SESSION_URL, params=params)
        except httpx.HTTPError as exc:
            logger.error("code2session 请求失败: %s", exc)
            raise AuthError("无法连接微信服务，请检查服务器出网连通性", status_code=502) from exc

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            raise AuthError("微信返回了非预期内容", status_code=502) from None

        errcode = int(data.get("errcode") or 0)
        if errcode:
            text = _ERR_TEXT.get(errcode) or data.get("errmsg") or "微信登录失败"
            logger.warning("code2session 失败: %s %s", errcode, data.get("errmsg"))
            raise ValidationError(text, code=f"wechat_{errcode}")
        if not data.get("openid"):
            logger.error("code2session 未返回 openid: %s", data)
            raise AuthError("微信未返回 openid，登录失败", status_code=502)
        return data


_wechat: Optional[WeChatService] = None


def get_wechat_service() -> WeChatService:
    global _wechat
    if _wechat is None:
        _wechat = WeChatService()
    return _wechat
