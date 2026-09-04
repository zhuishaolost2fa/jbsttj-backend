"""运营通知：把「有人求解析」这类运营事件推送到微信。

设计原则：

1. **旁路能力** —— 通知是锦上添花，绝不能影响主流程。所有对外请求都带短超时，
   任何异常都在本模块内吞掉并记录日志，调用方拿到的只是一个结果对象；
2. **未配置即降级** —— ``NOTIFY_ENABLED=false`` 或 ``NOTIFY_CHANNEL=none`` 时，
   只写日志不发包，本地开发 / 测试环境零副作用；
3. **渠道可插拔** —— 新增渠道只需在 ``_DISPATCH`` 里注册一个协程，
   service 层面对的是统一的 ``send(title, content)``。

渠道选型（都是「关注公众号 / 建个机器人 → 拿一个 key」级别的成本）：

- ``pushplus``：pushplus.plus，微信扫码拿 token，服务号直推个人微信，**最省事，推荐**；
- ``serverchan``：sct.ftqq.com（Server酱），拿 SendKey，方糖服务号直推；
- ``wecom_bot``：企业微信群机器人 webhook，消息进群，需要装企业微信；
- ``wecom_app``：企业微信自建应用消息，可推给指定成员，配置最重。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger("app.notifier")

# 通知文案里的时间统一用北京时间 —— 收消息的是中国人，UTC 看着要心算
_CST = timezone(timedelta(hours=8))

# 企业微信 access_token 有效期 7200s，提前 5 分钟刷新，避免踩点失效
_WECOM_TOKEN_TTL = 7200 - 300

# 各渠道高频错误码 → 可操作的下一步。
# 来源：渠道文档 + 实测（pushplus 905 是实测撞到的：未实名不让发）。
# 没收录的错误码会直接透出渠道原始 msg，不做猜测。
_CHANNEL_HINTS: Dict[str, Dict[int, str]] = {
    "pushplus": {
        600: "token 无效 —— 去 pushplus.plus 首页重新复制完整的 token",
        601: "token 已被重置或过期，重新复制一次",
        902: "参数格式错误，检查标题 / 正文是否为空",
        905: "账户未实名认证 —— 打开 https://verify.pushplus.plus 实名后才能发送；不想实名就换 serverchan 或 wecom_bot 渠道",
    },
    "serverchan": {
        40001: "SendKey 无效或已被重置 —— 去 sct.ftqq.com 重新复制",
        40002: "请求参数错误，检查标题 / 正文是否为空",
        40003: "今日发送额度已用完，等次日重置或升级套餐",
    },
    "wecom_bot": {
        93000: "webhook key 失效 —— 去群设置 → 群机器人重新复制 Webhook",
        45009: "接口调用超过频率限制，稍后重试",
    },
    "wecom_app": {
        40013: "CorpID 无效，去企业微信后台「我的企业」核对",
        41001: "CorpSecret 不对，或缺少 access_token",
        60020: "IP 不在白名单 —— 企业微信后台需配「可信IP」，Railway 出口 IP 会变，建议改用 wecom_bot",
        81013: "接收人 UserID 不存在 —— 改成 @all 或填正确的成员 UserID",
    },
}


@dataclass
class NotifyResult:
    """一次推送的结果。调用方只关心 ok，不关心渠道细节。"""

    ok: bool
    channel: str
    title: str = ""
    detail: str = ""
    error: Optional[str] = None
    elapsed_ms: int = 0
    payload: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - 仅日志用
        flag = "OK" if self.ok else "FAIL"
        extra = self.detail or self.error or ""
        return f"[{flag}] channel={self.channel} {self.elapsed_ms}ms {extra}".strip()


class Notifier:
    """统一通知入口。同一个进程内共享一个实例（持有 httpx 连接池与 token 缓存）。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        # 同一剧本的推送合并窗口：match_key -> 上次推送时间戳
        self._recent: Dict[str, float] = {}
        self._wecom_token: Optional[str] = None
        self._wecom_token_at: float = 0.0

    # ================= 高层：业务事件 =================
    async def notify_script_request(
        self,
        *,
        script_title: str,
        user_id: str,
        reason: Optional[str] = None,
        script_id: Optional[str] = None,
        script_code: Optional[str] = None,
        in_library: bool = True,
        pending_count: int = 0,
        user_email: Optional[str] = None,
        match_key: Optional[str] = None,
        revived: bool = False,
    ) -> NotifyResult:
        """有人发起「求解析」的通知。

        失败一律降级为日志，不向上抛 —— 求解析已经落库成功，
        推送失败不该让用户（也不该让接口）感知到。
        """
        title, content = self._render_script_request(
            script_title=script_title,
            user_id=user_id,
            reason=reason,
            script_id=script_id,
            script_code=script_code,
            in_library=in_library,
            pending_count=pending_count,
            user_email=user_email,
            revived=revived,
        )

        # 合并窗口：同一剧本短时间内的重复求解析只推一次，防止刷屏
        if match_key and self._should_skip(match_key):
            logger.info("求解析通知命中合并窗口，跳过: %s", script_title)
            return NotifyResult(
                ok=True, channel=self.settings.notify_channel,
                title=title, detail="命中合并窗口，已跳过",
            )

        result = await self.send(title, content)
        if match_key and result.ok:
            self._recent[match_key] = time.monotonic()
        return result

    # ================= 底层：统一出口 =================
    async def send(self, title: str, content: str) -> NotifyResult:
        """按配置的渠道发一条消息。任何异常都在这里收敛。"""
        s = self.settings
        channel = (s.notify_channel or "none").lower()

        if not s.notify_enabled or channel == "none":
            logger.info("[notify:off] %s | %s", title, content.replace("\n", " / "))
            return NotifyResult(ok=True, channel="none", title=title, detail="未启用推送")

        missing = s.missing_notify_config()
        if missing:
            logger.warning(
                "通知渠道 %s 缺少配置 %s，本次只写日志: %s",
                channel, ", ".join(missing), title,
            )
            return NotifyResult(
                ok=False, channel=channel, title=title,
                error=f"缺少配置: {', '.join(missing)}",
            )

        handler = _DISPATCH.get(channel)
        if handler is None:  # 配置校验已挡过，兜底防御
            return NotifyResult(ok=False, channel=channel, title=title, error="未知渠道")

        started = time.monotonic()
        try:
            payload = await handler(self, title, content)
        except httpx.HTTPError as exc:
            return NotifyResult(
                ok=False, channel=channel, title=title, error=f"网络错误: {exc}",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - 通知绝不能把异常抛回主流程
            logger.exception("通知推送异常 channel=%s", channel)
            return NotifyResult(
                ok=False, channel=channel, title=title, error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        elapsed = int((time.monotonic() - started) * 1000)
        if self._is_success(channel, payload):
            result = NotifyResult(
                ok=True, channel=channel, title=title,
                elapsed_ms=elapsed, payload=payload,
            )
        else:
            # 失败时把渠道错误码翻译成人话 —— 这些渠道的 msg 普遍只说现象不说
            # 怎么办（如 pushplus 905「账户未进行实名认证」，不告诉你认证地址）
            result = NotifyResult(
                ok=False, channel=channel, title=title, elapsed_ms=elapsed,
                detail=f"渠道返回: {payload}",
                error=self._extract_error(channel, payload),
                payload=payload,
            )
        log = logger.info if result.ok else logger.warning
        log("通知推送 channel=%s %s", channel, result)
        return result

    @staticmethod
    def _extract_error(channel: str, payload: Dict[str, Any]) -> str:
        """把渠道响应翻译成一句可操作的话。

        拼接规则：``错误码 → 已知含义（查表）｜渠道原始 msg``。
        查不到就只回原始 msg，不做无根据的猜测。
        """
        raw = payload.get("msg") or payload.get("errmsg") or payload.get("message") or ""
        raw = str(raw).strip()

        code = payload.get("code")
        if code is None:
            code = payload.get("errcode")
        hint = ""
        if code is not None:
            try:
                hint = _CHANNEL_HINTS.get(channel, {}).get(int(code), "")
            except (TypeError, ValueError):
                hint = ""

        parts = [f"code={code}"] if code is not None else []
        if hint:
            parts.append(hint)
        if raw and raw not in hint:
            parts.append(raw)
        return "｜".join(parts) or "未知错误（渠道未返回错误信息）"

    # ================= 文案 =================
    def _render_script_request(
        self,
        *,
        script_title: str,
        user_id: str,
        reason: Optional[str] = None,
        script_id: Optional[str] = None,
        script_code: Optional[str] = None,
        in_library: bool = True,
        pending_count: int = 0,
        user_email: Optional[str] = None,
        revived: bool = False,
    ) -> tuple[str, str]:
        """组装标题与正文。纯文本优先 —— 各渠道对富文本支持参差，文本最稳。"""
        who = user_email or f"{user_id[:8]}…"
        tag = "重新求解析" if revived else "有人求解析"
        title = f"【{tag}】{script_title}"

        lines = [
            f"剧本：{script_title}",
            f"来源：{'剧本库内' if in_library else '库外（未在库中）'}",
        ]
        if script_code:
            lines.append(f"编码：{script_code}")
        if script_id:
            lines.append(f"剧本ID：{script_id}")
        lines.append(f"发起人：{who}")
        if reason:
            lines.append(f"理由：{reason}")
        if pending_count:
            lines.append(f"累计：已有 {pending_count} 人求过这本")
        lines.append(f"时间：{datetime.now(_CST):%Y-%m-%d %H:%M:%S}")
        lines.append(f"环境：{self.settings.app_env}")
        if self.settings.notify_console_url:
            lines.append(f"处理：{self.settings.notify_console_url}")
        return title, "\n".join(lines)

    # ================= 合并窗口 =================
    def _should_skip(self, match_key: str) -> bool:
        window = self.settings.notify_dedup_window
        if window <= 0:
            return False
        last = self._recent.get(match_key)
        if last is None or time.monotonic() - last >= window:
            return False
        # 顺带清理过期键，避免长期运行后字典无限膨胀
        if len(self._recent) > 500:
            expired = [k for k, v in self._recent.items() if time.monotonic() - v >= window]
            for k in expired:
                self._recent.pop(k, None)
        return True

    # ================= 渠道实现 =================
    async def _post_json(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        async with self._client() as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError:
                return {"code": resp.status_code, "raw": resp.text[:500]}
            return data if isinstance(data, dict) else {"data": data}

    async def _post_form(self, url: str, data: Dict[str, Any]) -> Dict[str, Any]:
        async with self._client() as client:
            resp = await client.post(url, data=data)
            resp.raise_for_status()
            try:
                payload = resp.json()
            except ValueError:
                return {"code": resp.status_code, "raw": resp.text[:500]}
            return payload if isinstance(payload, dict) else {"data": payload}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.notify_timeout, connect=5.0),
            headers={"User-Agent": "jbsttj-backend/1.0 (+notifier)"},
        )

    @staticmethod
    def _is_success(channel: str, payload: Dict[str, Any]) -> bool:
        """各渠道的成功判定。HTTP 200 不等于推送成功 —— 这几个渠道都在
        响应体里用 code 表达业务结果，只看状态码会被「鉴权失败」骗过去。"""
        code = payload.get("code")
        errcode = payload.get("errcode")
        if channel == "pushplus":
            return int(code or -1) == 200
        if channel == "serverchan":
            # 成功时 code 为 0；失败返回 {"code":40001,"message":"..."}
            return int(code if code is not None else -1) == 0
        if channel in {"wecom_bot", "wecom_app"}:
            return int(errcode if errcode is not None else -1) == 0
        return True

    # ---- PushPlus ----
    async def _send_pushplus(self, title: str, content: str) -> Dict[str, Any]:
        s = self.settings
        body: Dict[str, Any] = {
            "token": s.pushplus_token,
            "title": title,
            "content": content,
            "template": s.pushplus_template or "txt",
        }
        if s.pushplus_topic:
            body["topic"] = s.pushplus_topic
        return await self._post_json("https://www.pushplus.plus/send", body)

    # ---- Server酱 ----
    async def _send_serverchan(self, title: str, content: str) -> Dict[str, Any]:
        s = self.settings
        # SendKey 形如 SCT123456xxxx，直接拼进路径
        url = f"https://sctapi.ftqq.com/{s.serverchan_send_key.strip()}.send"
        data = {"title": title, "desp": content}
        if s.serverchan_channel:
            data["channel"] = s.serverchan_channel
        return await self._post_form(url, data)

    # ---- 企业微信群机器人 ----
    async def _send_wecom_bot(self, title: str, content: str) -> Dict[str, Any]:
        s = self.settings
        mobiles = [m.strip() for m in (s.wecom_bot_mentioned_mobiles or "").split(",") if m.strip()]
        text = f"{title}\n{content}"
        body: Dict[str, Any] = {
            "msgtype": "text",
            "text": {"content": text, "mentioned_mobile_list": mobiles},
        }
        return await self._post_json(s.wecom_bot_webhook.strip(), body)

    # ---- 企业微信应用消息 ----
    async def _send_wecom_app(self, title: str, content: str) -> Dict[str, Any]:
        s = self.settings
        token = await self._wecom_access_token()
        if not token:
            return {"errcode": -1, "errmsg": "获取 access_token 失败"}
        body = {
            "touser": s.wecom_to_user or "@all",
            "msgtype": "text",
            "agentid": s.wecom_agent_id,
            "text": {"content": f"{title}\n{content}"},
        }
        return await self._post_json(
            "https://qyapi.weixin.qq.com/cgi-bin/message/send",
            {**body, "access_token": token},
        )

    async def _wecom_access_token(self) -> Optional[str]:
        """带缓存地换取企业微信 access_token（有效期 2 小时）。"""
        if self._wecom_token and time.monotonic() - self._wecom_token_at < _WECOM_TOKEN_TTL:
            return self._wecom_token
        s = self.settings
        url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
        async with self._client() as client:
            resp = await client.get(
                url, params={"corpid": s.wecom_corp_id, "corpsecret": s.wecom_corp_secret}
            )
            resp.raise_for_status()
            data = resp.json()
        token = data.get("access_token")
        if not token:
            logger.warning("企业微信换取 access_token 失败: %s", data)
            return None
        self._wecom_token = token
        self._wecom_token_at = time.monotonic()
        return token


_DISPATCH = {
    "pushplus": Notifier._send_pushplus,
    "serverchan": Notifier._send_serverchan,
    "wecom_bot": Notifier._send_wecom_bot,
    "wecom_app": Notifier._send_wecom_app,
}

_notifier: Optional[Notifier] = None


def get_notifier() -> Notifier:
    global _notifier
    if _notifier is None:
        _notifier = Notifier()
    return _notifier
