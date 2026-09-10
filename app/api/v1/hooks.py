"""Supabase Auth **Send Email Hook** 接收端。

启用方式（Supabase Dashboard → Authentication → Hooks → Send Email）：
    URI      https://<你的域名>/api/v1/hooks/supabase/send-email
    Secret   v1,whsec_xxx（同一个值配到本服务的 SEND_EMAIL_HOOK_SECRETS）

为什么需要它
------------
Supabase 内置 SMTP 只有 **2 封/小时** 且不可调，注册确认 / 找回密码 / magiclink /
OTP / 改邮箱全部共用这一个池子，第二封注册就会 429。启用本 hook 后 GoTrue 不再经
SMTP，而是把邮件内容 POST 给我们自己投递，配额随之变成我们邮件服务商的配额。
腾讯云 SES 的个人实名账号自 2026-03-02 起**不再提供 SMTP**，只能调 API —— 这也是
这里做成 HTTP hook、而不是在后台配 SMTP 的直接原因。

安全模型
--------
请求按 Standard Webhooks 规范携带 HMAC-SHA256 签名（`webhook-id` /
`webhook-timestamp` / `webhook-signature` 三个头）。payload 里含**可直接登录的
凭证**（token_hash），而端点必然公网可达，所以验签失败一律 401：
绝不「先记日志再放行」。未配 secret 时直接 503，避免裸奔上线。

时间预算
--------
GoTrue 重试 3 次、总预算 5 秒，所以这里同步发送是刻意的 —— 丢进队列会把链路
拉长到不可控。SMTP provider 因此走线程池，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi import status as http_status

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigError

logger = logging.getLogger("app.hooks")

router = APIRouter(prefix="/hooks", tags=["Supabase Hooks"])

# Standard Webhooks 规定的时间戳容差，超时请求按重放攻击丢弃
_WEBHOOK_TOLERANCE_SECONDS = 300
# 一眼能认出是「6 位验证码」而非 uuid-like  token 的形态
_OTP_TOKEN_RE = re.compile(r"^\d{4,8}$")
# 多密钥轮转的分隔点：只有后面还跟着下一个 secret 的逗号才切
_MULTI_SECRET_SPLIT_RE = re.compile(r",(?=v1,whsec_)")

_ACTION_SUBJECTS = {
    "signup": "请验证你的邮箱",
    "recovery": "重置你的密码",
    "magiclink": "你的登录链接",
    "invite": "你被邀请加入",
    "email_change": "确认更换邮箱",
    "reauthentication": "你的安全验证码",
}


# ---------------------------------------------------------------- 验签


def _parse_webhook_secret(raw: str) -> bytes:
    """把 `v1,whsec_<base64>` 还原成 HMAC key。"""
    secret = raw.strip()
    if "," in secret:
        secret = secret.split(",", 1)[1].strip()
    if secret.startswith("whsec_"):
        secret = secret[len("whsec_") :]
    return base64.b64decode(secret)


def _load_hook_secrets(settings: Settings) -> List[str]:
    """切出候选 secret 列表（支持多密钥轮转）。

    ⚠️ 不能按逗号无脑切：单个 secret 的格式本身就是 ``v1,whsec_<base64>``，
    无脑 split 会把 ``['v1', 'whsec_xxx']`` 拿去做 HMAC，结果永远对不上。
    只在「后面还跟着下一个 v1,whsec_」的逗号处下刀，单密钥场景不受影响。
    """
    raw = (settings.send_email_hook_secrets or "").strip()
    if not raw:
        return []
    return [item.strip() for item in _MULTI_SECRET_SPLIT_RE.split(raw) if item.strip()]


def verify_standard_webhook(
    body: bytes,
    headers: Dict[str, str],
    secrets: List[str],
    *,
    now: Optional[float] = None,
    tolerance: int = _WEBHOOK_TOLERANCE_SECONDS,
) -> Tuple[bool, str]:
    """校验 Standard Webhooks 签名，返回 ``(ok, reason)``。

    签名内容是把 ``webhook-id``、``webhook-timestamp``、请求体用 ``.`` 拼接后
    再做 HMAC-SHA256，结果 base64 编码并加 ``v1,`` 前缀；头里可能带多个候选版本
    （空格分隔，便于密钥轮转），任一命中即通过。
    """
    msg_id = headers.get("webhook-id", "")
    timestamp = headers.get("webhook-timestamp", "")
    signatures = headers.get("webhook-signature", "")
    if not (msg_id and timestamp and signatures):
        return False, "missing_signature_headers"
    try:
        ts = int(timestamp)
    except ValueError:
        return False, "bad_timestamp"
    if abs((now if now is not None else time.time()) - ts) > tolerance:
        return False, "timestamp_out_of_tolerance"

    signed_content = f"{msg_id}.{timestamp}.".encode() + body
    for secret in secrets:
        try:
            key = _parse_webhook_secret(secret)
        except Exception:  # noqa: BLE001 - base64 解失败等同配置错误，换下一个候选
            continue
        expected = "v1," + base64.b64encode(
            hmac.new(key, signed_content, hashlib.sha256).digest()
        ).decode()
        if expected in signatures.split(" "):
            return True, "ok"
    return False, "signature_mismatch"


# ---------------------------------------------------------------- 投放


def _render_email(action: str, token: str, verify_url: str, brand: str = "") -> Tuple[str, str]:
    """返回 ``(subject, text_body)``。

    Supabase 给的是原始 token，**链接要我们自己拼** —— 后台模板里的
    ``{{ .ConfirmationURL }}`` 就是这么来的。token 形如 6 位数字时按验证码走，
    同时附上链接，两种时区/客户端都能用。
    """
    subject = _ACTION_SUBJECTS.get(action, "账号验证")
    if brand:
        subject = f"【{brand}】{subject}"
    if _OTP_TOKEN_RE.match(token or ""):
        body = (
            f"你的验证码是：{token}\n\n"
            f"也可以直接点下面的链接完成操作（{30} 分钟内有效）：\n{verify_url}\n\n"
            "如果这不是你本人的操作，请忽略本邮件。"
        )
    else:
        body = (
            f"请点击下面的链接完成「{subject}」（{30} 分钟内有效）：\n{verify_url}\n\n"
            "如果这不是你本人的操作，请忽略本邮件。"
        )
    return subject, body


def _send_via_smtp(settings: Settings, to_email: str, subject: str, body_text: str) -> None:
    """通用 SMTP 投递（标准库实现，不引入依赖）。

    注意这是**同步阻塞**调用，只能放在线程池里跑，不能直接 await。
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{settings.mail_from_name} <{settings.mail_from}>" if settings.mail_from_name else settings.mail_from
    msg["To"] = to_email
    msg.set_content(body_text)

    if settings.mail_smtp_use_ssl:
        server = smtplib.SMTP_SSL(settings.mail_smtp_host, settings.mail_smtp_port, timeout=10,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(settings.mail_smtp_host, settings.mail_smtp_port, timeout=10)
    try:
        if not settings.mail_smtp_use_ssl:
            # 587 端口：先明文握手再 STARTTLS 升级
            server.starttls(context=ssl.create_default_context())
        if settings.mail_smtp_user:
            server.login(settings.mail_smtp_user, settings.mail_smtp_pass)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 - 关闭失败不影响已投递的结果
            pass


def _send_via_tencentcloud(
    settings: Settings, to_email: str, subject: str, body_text: str,
    *, template_data: Optional[Dict[str, str]] = None,
) -> None:
    """腾讯云 SES **API** 投递。

    之所以不用 SMTP：腾讯云邮件推送自 2026-03-02 起，新开通的个人实名账号
    不再支持 SMTP 发信（只能用 API 或控制台）。SDK 是可选依赖，没装就明确报错。

    ⚠️ 腾讯云默认**只允许模板发信**：`Simple` 字段对未申请过特殊配置的账号已废弃，
    传了会报 ``FailedOperation.WithOutPermission``。所以默认走 Template，
    只有在确实拿到 Simple 权限且没配 template_id 时才回退。
    """
    try:
        from tencentcloud.common import credential  # type: ignore
        from tencentcloud.common.profile.client_profile import ClientProfile  # type: ignore
        from tencentcloud.common.profile.http_profile import HttpProfile  # type: ignore
        from tencentcloud.ses.v20201002 import models, ses_client  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise ConfigError(
            "MAIL_PROVIDER=tencentcloud 需要安装 SDK：pip install tencentcloud-sdk-python-ses"
        ) from exc

    cred = credential.Credential(settings.tencentcloud_secret_id, settings.tencentcloud_secret_key)
    http_profile = HttpProfile(endpoint="ses.tencentcloudapi.com", reqTimeout=10)
    client = ses_client.SesClient(cred, settings.tencentcloud_ses_region,
                                  ClientProfile(httpProfile=http_profile))

    import json as _json

    req = models.SendEmailRequest()
    req.FromEmailAddress = settings.mail_from
    req.Destination = [to_email]
    req.Subject = subject
    # 触发类（验证码）走专用通道，投递优先级高于营销类
    req.TriggerType = 1

    if settings.tencentcloud_ses_template_id:
        req.Template = models.Template()
        req.Template.TemplateID = settings.tencentcloud_ses_template_id
        req.Template.TemplateData = _json.dumps(template_data or {}, ensure_ascii=False)
    else:
        # 只有历史上申请过 Simple 权限的账号能用；新账号这里会被腾讯云拒绝
        req.Simple = models.Simple()
        req.Simple.Text = body_text
    client.SendEmail(req)


async def _dispatch(
    settings: Settings,
    to_email: str,
    subject: str,
    body_text: str,
    *,
    template_data: Optional[Dict[str, str]] = None,
) -> None:
    provider = (settings.mail_provider or "none").strip().lower()
    if provider == "none":
        # 未配置通道时只记日志：方便先接通 hook 验签链路，再单独调邮件通道
        logger.info("未启用邮件通道（MAIL_PROVIDER=none），跳过投递 to=%s subject=%s", to_email, subject)
        return
    if provider == "smtp":
        await asyncio.to_thread(_send_via_smtp, settings, to_email, subject, body_text)
        return
    if provider == "tencentcloud":
        await asyncio.to_thread(
            _send_via_tencentcloud, settings, to_email, subject, body_text,
            template_data=template_data,
        )
        return
    raise ConfigError(f"未知的 MAIL_PROVIDER：{provider}")


# ---------------------------------------------------------------- 路由


def _verify_url(email_data: Dict[str, Any], token_hash: str, action: str) -> str:
    """按 GoTrue 默认模板的规则拼确认链接。"""
    site_url = (email_data.get("site_url") or "").rstrip("/")
    redirect_to = email_data.get("redirect_to") or ""
    url = f"{site_url}/auth/v1/verify?token={token_hash}&type={action}"
    if redirect_to:
        url += f"&redirect_to={redirect_to}"
    return url


@router.post("/supabase/send-email", summary="Supabase Send Email Hook 接收端")
async def supabase_send_email(request: Request) -> Response:
    """接收 GoTrue 的邮件投递请求，用我们自己的通道发出去。

    ⚠️ 必须用 Standard Webhooks 签名鉴权 —— payload 里是能直接兑换会话的凭证。
    """
    settings = get_settings()
    secrets = _load_hook_secrets(settings)
    if not secrets:
        # 宁可 503 也不裸奔：没配 secret 的 hook 端点是个开放转发器
        logger.error("收到 Send Email Hook，但 SEND_EMAIL_HOOK_SECRETS 未配置")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Send Email Hook 未配置",
        )

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    ok, reason = verify_standard_webhook(body, headers, secrets)
    if not ok:
        logger.warning("Send Email Hook 验签失败（%s），来源 %s", reason, request.client.host if request.client else "-")
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="invalid signature")

    payload: Dict[str, Any] = await request.json()
    user = payload.get("user") or {}
    email_data = payload.get("email_data") or {}
    action = email_data.get("email_action_type") or "signup"

    send_to = user.get("email") or ""
    token = email_data.get("token") or ""
    token_hash = email_data.get("token_hash") or ""

    if action == "email_change":
        # ⚠️ Supabase 的字段名是反的（为兼容历史版本）：Secure Email Change 开启时会
        # 生成两组 token，token_hash_new 对应的其实是**当前**邮箱，token_hash 才是新邮箱。
        # 搞反了就会把验证链路发到错误地址，用户永远验证不了。
        new_email = user.get("new_email") or email_data.get("new_email") or ""
        if new_email and email_data.get("token_new"):
            send_to = new_email
            token = email_data["token_new"]
            token_hash = email_data.get("token_hash") or token_hash

    if not send_to:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="missing recipient")

    # 诊断用：只有看到真实 token 形态，才能确定该用「验证码模板」还是「链接模板」
    logger.info(
        "send-email hook action=%s to=%s token_len=%s token_is_otp=%s keys=%s",
        action, send_to, len(token), bool(_OTP_TOKEN_RE.match(token)), sorted(email_data.keys()),
    )
    verify_url = _verify_url(email_data, token_hash, action)
    subject, body_text = _render_email(action, token, verify_url, brand=settings.mail_brand_name)

    # 控制台模板正文里写成 {{code}} / {{url}} / {{url_query}}（双花括号）。
    # url_query 是为腾讯云审核规则准备的：链接域名必须写死在模板里，只有查询串能做变量。
    url_query = verify_url.split("?", 1)[1] if "?" in verify_url else ""
    template_data = {
        settings.tencentcloud_ses_template_var_code: token,
        settings.tencentcloud_ses_template_var_url: verify_url,
        settings.tencentcloud_ses_template_var_url_query: url_query,
    }

    # GoTrue 只等 5 秒；投递失败必须抛出来让它重试，绝不能静默吞掉
    try:
        await _dispatch(settings, send_to, subject, body_text, template_data=template_data)
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("投递 auth 邮件失败（action=%s to=%s）：%s", action, send_to, exc)
        raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail="投递失败")

    logger.info("已投递 auth 邮件（action=%s to=%s provider=%s）", action, send_to, settings.mail_provider)
    return Response(status_code=200, content="{}", media_type="application/json")
