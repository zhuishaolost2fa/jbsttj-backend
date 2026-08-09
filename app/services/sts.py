"""阿里云 STS 临时凭证服务。

设计目标
--------
长期 AccessKey（OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET）只留在服务端，
用它调用 STS 的 `AssumeRole` 去扮演 `OSS_STS_ROLE_ARN` 指定的 RAM 角色，
拿到**短期、可过期**的临时凭证（AccessKeyId + AccessKeySecret + SecurityToken）。

拿到临时凭证后有两种用法：
  1. 服务端自用：把临时凭证喂给 OSS 客户端（本文件提供 `as_oss_credentials()`，
     由 `OSSService` 通过 `CredentialsProviderFunc` 接入，临时凭证临近过期会自动刷新）。
  2. 下发给浏览器：通过 `POST /api/v1/sts/token` 把临时凭证交给前端，
     前端即可用 OSS 浏览器 SDK 直传（长期密钥永不离开服务端）。

注意：STS 官方 SDK（alibabacloud_sts20150401）为同步阻塞，所有网络调用
统一丢进线程池；同时用线程锁保护临时凭证的缓存与刷新。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.exceptions import ConfigError, StorageError

logger = logging.getLogger("app.sts")

# 官方 STS SDK 为可选依赖：未安装时，只要不真正开启 STS 就不影响应用启动。
try:
    from alibabacloud_sts20150401.client import Client as StsClient
    from alibabacloud_sts20150401 import models as sts_models
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util import models as util_models

    _HAS_STS_SDK = True
except Exception:  # pragma: no cover - 仅在未安装 SDK 时触发
    _HAS_STS_SDK = False


@dataclass
class STSCredentials:
    access_key_id: str
    access_key_secret: str
    security_token: str
    expiration: datetime  # 带时区的 UTC 时间

    @property
    def expired(self, buffer_seconds: int = 300) -> bool:
        """是否即将过期（提前 buffer_seconds 秒即视为需要刷新）。"""
        return datetime.now(timezone.utc) >= (self.expiration - timedelta(seconds=buffer_seconds))


class STSClient:
    """AssumeRole 临时凭证客户端，带线程安全的缓存与自动刷新。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings
        self._lock = threading.Lock()
        self._cached: Optional[STSCredentials] = None
        self._client: Optional["StsClient"] = None
        if settings is not None and settings.oss_use_sts:
            self._client = self._build_client()

    # ------------------------------------------------------------------
    # 客户端构建
    # ------------------------------------------------------------------
    def _build_client(self) -> "StsClient":
        if not _HAS_STS_SDK:
            raise ConfigError(
                "使用 STS 必须先安装官方 SDK：`pip install alibabacloud-sts20150401`",
                code="sts_sdk_missing",
            )
        s = self._s
        ak = s.oss_sts_access_key_id or s.oss_access_key_id
        sk = s.oss_sts_access_key_secret or s.oss_access_key_secret
        if not ak or not sk:
            raise ConfigError("STS 调用方长期 AccessKey 未配置（OSS_ACCESS_KEY_ID/SECRET 或 STS 专用键）")
        if not s.oss_sts_role_arn:
            raise ConfigError("STS 角色 ARN 未配置（OSS_STS_ROLE_ARN）")
        cfg = open_api_models.Config(access_key_id=ak, access_key_secret=sk)
        cfg.endpoint = s.oss_sts_endpoint
        return StsClient(cfg)

    # ------------------------------------------------------------------
    # 获取 / 刷新临时凭证（同步，供 OSS CredentialsProviderFunc 调用）
    # ------------------------------------------------------------------
    def get_credentials_sync(self) -> STSCredentials:
        """返回当前有效的临时凭证，临近过期时阻塞刷新。

        该方法在 OSS 客户端内部被同步调用（本身已在线程池中），
        因此内部的阻塞式 AssumeRole 不会阻塞事件循环。
        """
        with self._lock:
            if self._cached is None or self._cached.expired:
                self._cached = self._assume_role_sync()
            return self._cached

    def _assume_role_sync(self) -> STSCredentials:
        """同步调用 STS AssumeRole。

        本方法在 OSS 客户端内部被同步调用，而 OSS 的每个网络操作本身已被
        `run_in_threadpool` 丢进线程池，因此这里直接调用阻塞式 STS SDK 即可，
        不会阻塞事件循环。
        """
        if self._client is None:
            self._client = self._build_client()
        s = self._s
        req = sts_models.AssumeRoleRequest(
            role_arn=s.oss_sts_role_arn,
            role_session_name=s.oss_sts_session_name,
            duration_seconds=s.oss_sts_duration_seconds,
        )
        runtime = util_models.RuntimeOptions()
        try:
            resp = self._client.assume_role_with_options(req, runtime)
        except Exception as exc:  # noqa: BLE001
            logger.exception("STS AssumeRole 失败")
            raise StorageError(f"STS AssumeRole 失败: {exc}", code="sts_assume_failed") from exc

        body = resp.body
        if body is None or body.credentials is None:
            raise StorageError("STS AssumeRole 返回为空", code="sts_empty")
        cred = body.credentials
        exp_raw = cred.expiration or ""
        try:
            # 形如 2026-08-06T15:00:00Z
            exp = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
        except Exception:
            # 兜底：以当前时间为基准 + duration
            exp = datetime.now(timezone.utc) + timedelta(seconds=s.oss_sts_duration_seconds)
        return STSCredentials(
            access_key_id=cred.access_key_id or "",
            access_key_secret=cred.access_key_secret or "",
            security_token=cred.security_token or "",
            expiration=exp,
        )

    # ------------------------------------------------------------------
    # 适配为 OSS V2 的 Credentials（供 CredentialsProviderFunc 使用）
    # ------------------------------------------------------------------
    def as_oss_credentials(self):
        """返回 OSS V2 SDK 需要的 `oss.credentials.Credentials` 对象。"""
        import alibabacloud_oss_v2 as oss

        creds = self.get_credentials_sync()
        return oss.credentials.Credentials(
            access_key_id=creds.access_key_id,
            access_key_secret=creds.access_key_secret,
            security_token=creds.security_token,
            expiration=creds.expiration,
        )


_sts_client: Optional["STSClient"] = None


def get_sts_client(settings: Optional[Settings] = None) -> Optional["STSClient"]:
    global _sts_client
    if _sts_client is None:
        st = settings or _get_settings_safe()
        if st is None or not st.oss_use_sts:
            return None
        _sts_client = STSClient(st)
    return _sts_client


def _get_settings_safe() -> Optional[Settings]:
    try:
        from app.core.config import get_settings

        return get_settings()
    except Exception:  # pragma: no cover
        return None
