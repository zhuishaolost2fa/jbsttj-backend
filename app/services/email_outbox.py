"""认证邮件发件箱轮询器（反向拉取）。

为什么不是 Supabase 把邮件事件推给我们：
  服务器域名未备案，腾讯云对国际链路上的域名访问做了拦截；更致命的是
  **Supabase 所在区域（新加坡）到我们服务器 80 端口的 TCP 握手直接超时**
  （pg_net 实测 12s 超时：DNS 0.02ms、TCP handshake 12000ms）。推模式无论走
  HTTP Hook 还是 Edge Function 都会撞同一堵墙。

所以我们反过来：GoTrue 的 Send Email Hook 用 Postgres 函数把事件写进
``public.auth_email_outbox``，本服务（出网方向畅通，本来就一直在调 Supabase）
定时去表里**拉**未发送的条目，用腾讯云 SES 发出去，再回写 sent_at。

代价是邮件延迟 = 轮询间隔（默认 3 秒），对注册验证完全可接受。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from app.core.config import Settings, get_settings
from app.services.supabase import SupabaseClient, get_supabase

logger = logging.getLogger("app.email_outbox")

TABLE = "auth_email_outbox"


class EmailOutboxPoller:
    """后台轮询发件箱。只有真正配置了邮件通道才启动。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self._consecutive_errors = 0

    @property
    def enabled(self) -> bool:
        provider = (self._settings.mail_provider or "none").strip().lower()
        return provider != "none" and self._settings.supabase_service_role_key != ""

    async def start(self) -> None:
        if not self.enabled:
            logger.info("发件箱轮询未启用（MAIL_PROVIDER=%s）", self._settings.mail_provider)
            return
        if self._task and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="email-outbox-poller")
        logger.info(
            "发件箱轮询已启动（间隔 %ss，表 %s）",
            self._settings.auth_email_poll_interval_seconds, TABLE,
        )

    async def stop(self) -> None:
        if not self._task:
            return
        self._stopped.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None

    async def _loop(self) -> None:
        interval = max(1, self._settings.auth_email_poll_interval_seconds)
        db = get_supabase()
        while not self._stopped.is_set():
            try:
                await self._drain(db)
                self._consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._consecutive_errors += 1
                # 前几次小声点，连续失败再升级成 error，避免刷屏
                level = logger.error if self._consecutive_errors >= 3 else logger.warning
                level("发件箱轮询异常（第 %s 次）：%s", self._consecutive_errors, exc)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _drain(self, db: SupabaseClient) -> None:
        if not db.available:
            return
        rows: List[Dict[str, Any]] = await db.select(
            TABLE,
            filters={"sent_at": "is.null"},
            order="id.asc",
            limit=self._settings.auth_email_poll_batch_size,
        )
        if not rows:
            return

        # 延迟导入：避免 app.api 层在启动时被循环拉进来
        from app.api.v1.hooks import deliver_email_payload

        for row in rows:
            row_id = row.get("id")
            payload = row.get("payload") or {}
            try:
                await deliver_email_payload(self._settings, payload)
                await db.update(TABLE, filters={"id": f"eq.{row_id}"}, data={"sent_at": "now()"})
            except Exception as exc:  # noqa: BLE001
                logger.error("发件箱 %s 投递失败：%s", row_id, exc)
                try:
                    await db.update(
                        TABLE,
                        filters={"id": f"eq.{row_id}"},
                        data={"error": str(exc)[:500]},
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("回写发件箱错误失败（id=%s）", row_id)


_poller: Optional[EmailOutboxPoller] = None


async def start_email_outbox_poller() -> None:
    global _poller
    if _poller is None:
        _poller = EmailOutboxPoller()
    await _poller.start()


async def stop_email_outbox_poller() -> None:
    global _poller
    if _poller is not None:
        await _poller.stop()
