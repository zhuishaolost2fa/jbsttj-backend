"""微信推送自检：验证求解析通知的渠道配置是否可用。

用法（在项目根目录执行）：

    python scripts/test_notify.py              # 发一条测试消息
    python scripts/test_notify.py --dry-run    # 只拼文案不发包

它会做三件事：
1. 打印当前 NOTIFY_* 配置与缺失项；
2. 用真实的「求解析」文案发一条消息（走与线上完全相同的渲染逻辑）；
3. 打印渠道返回的原始 payload，失败时给可操作的排查提示。

退出码：0=推送成功；1=失败或未配置。
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.services.notifier import get_notifier  # noqa: E402

# 各渠道的凭证获取地址，失败时直接给出链接
_HOME = {
    "pushplus": "pushplus.plus",
    "serverchan": "sct.ftqq.com",
}


def _mask(v: str, keep: int = 6) -> str:
    """凭证打码，避免自检输出里泄露 token。"""
    if not v:
        return "(空)"
    if len(v) <= keep:
        return "*" * len(v)
    return f"{v[:keep]}{'*' * (len(v) - keep)}"


def main() -> int:
    s = get_settings()
    channel = (s.notify_channel or "none").lower()

    print("=== 通知配置 ===")
    print(f"  总开关 NOTIFY_ENABLED      : {s.notify_enabled}")
    print(f"  渠道   NOTIFY_CHANNEL      : {channel}")
    print(f"  超时   NOTIFY_TIMEOUT      : {s.notify_timeout}s")
    print(f"  合并窗口 NOTIFY_DEDUP_WINDOW: {s.notify_dedup_window}s")
    print(f"  复活通知 NOTIFY_ON_REVIVE   : {s.notify_on_revive}")
    print(f"  环境   APP_ENV             : {s.app_env}")

    if channel == "pushplus":
        print(f"  PUSHPLUS_TOKEN : {_mask(s.pushplus_token)}")
    elif channel == "serverchan":
        print(f"  SERVERCHAN_SEND_KEY : {_mask(s.serverchan_send_key)}")
    elif channel == "wecom_bot":
        print(f"  WECOM_BOT_WEBHOOK : {_mask(s.wecom_bot_webhook, 40)}")
    elif channel == "wecom_app":
        print(f"  WECOM_CORP_ID : {_mask(s.wecom_corp_id)}")
        print(f"  WECOM_AGENT_ID: {s.wecom_agent_id or '(空)'}")
        print(f"  WECOM_TO_USER : {s.wecom_to_user or '(空)'}")

    if not s.notify_enabled:
        print("\n[跳过] NOTIFY_ENABLED=false，通知只写日志。")
        print("      在 .env / Railway 里设 NOTIFY_ENABLED=true 后重跑本脚本。")
        return 1
    if channel == "none":
        print("\n[跳过] NOTIFY_CHANNEL=none。可选：pushplus / serverchan / wecom_bot / wecom_app")
        return 1

    missing = s.missing_notify_config()
    if missing:
        print(f"\n[失败] 渠道 {channel} 缺少配置: {', '.join(missing)}")
        return 1

    notifier = get_notifier()
    title, content = notifier._render_script_request(  # noqa: SLF001 - 自检脚本直接用私有渲染
        script_title="病娇男孩的精分日记（测试消2息）",
        user_id="00000000-0000-0000-0000-000000000000",
        reason="这是一条来自 scripts/test_notify.py 的连通性测试消息",
        script_code="bing-jiao-nan-hai",
        in_library=True,
        pending_count=1,
        user_email="tester@example.com",
        revived=False,
    )
    print("\n=== 待发送文案 ===")
    print(f"标题: {title}")
    print(content)

    if "--dry-run" in sys.argv:
        print("\n[dry-run] 已跳过实际发送。")
        return 0

    print("\n=== 发送中 ===")
    result = asyncio.run(notifier.send(title, content))
    print(f"结果: {result}")
    if result.payload:
        print(f"渠道原始返回: {result.payload}")

    if not result.ok:
        print("\n=== 失败原因 ===")
        print(f"  {result.error}")
        print("\n=== 仍收不到消息时的兜底检查 ===")
        if channel in {"pushplus", "serverchan"}:
            print(f"  - 需在 {_HOME[channel]} 里完成扫码登录，并关注对应的微信服务号，否则消息无处可投；")
        elif channel == "wecom_bot":
            print("  - 消息进「企业微信群」，微信里要通过企业微信才能看到；")
        elif channel == "wecom_app":
            print("  - 消息发给「企业微信」成员，微信里需装企业微信并绑定。")
        print("  - 凭证是否带了多余空格 / 引号；环境变量改动后是否已重启服务。")
        return 1

    print("\n[成功] 请查看微信是否收到消息。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
