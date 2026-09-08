"""微信小程序服务端接口。

只依赖 httpx，不引第三方 SDK。登录链路上的外部调用必须**短超时 + 明确报错**，
否则微信接口一慢会直接拖垮 /auth/wechat/login。

本模块刻意只保留登录必需的能力：
  - code2session：wx.login 的 code 换 openid / unionid / session_key
  - placeholder_email / random_password：建号时凑齐 GoTrue 要求的材料

没有实现 getuserphonenumber：它要求企业主体 + 认证 + 单独付费，个人主体用不了，
且本项目不做手机号绑定，引进来只是死代码。

关于**为什么不再派生确定性密码**：早期实现用 HMAC(openid) 派生的固定密码走
password grant。但 GoTrue 一个账号只有一个密码 —— 微信用户一旦绑定真实邮箱
并设置自己的密码，两种登录方式就会互相顶掉。现在登录改用 magiclink 免密签发
（见 SupabaseAuth.issue_session_for_user），密码只在建号时随机生成一次，此后
不再使用，也就不存在冲突。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import string
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
    @staticmethod
    def random_password() -> str:
        """建号时用的随机密码，**生成后不保存**。

        GoTrue 建号要求带密码，但登录改走 magiclink 免密签发后用不到它。
        刻意不保存、也不从 openid 派生：一旦可复现，就等于给账号留了一个
        绕过微信身份校验的后门。
        """
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        return "Jbs!" + "".join(secrets.choice(alphabet) for _ in range(28))

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
