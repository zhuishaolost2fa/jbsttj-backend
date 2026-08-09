"""阿里云 OSS V2 SDK 封装（分片上传 / 预签名 / 下载 / 删除）。

使用官方新版 SDK `alibabacloud_oss_v2`（自带 V4 签名）。该 SDK 公开 API 为同步阻塞，
所有网络调用统一丢进线程池 `run_in_threadpool`，避免阻塞事件循环。

两个客户端实例的区别：
  * client      —— 服务端自身调用 OSS（init/upload/list/complete/abort/put/head/delete），
                  优先走内网 endpoint（同地域免流量费）
  * sign_client —— 生成给浏览器的签名 URL 用，必须是公网可达的 endpoint 或 CDN 域名

关于分片直传 Content-Type（V2 SDK 的一个坑）：
  `UploadPartRequest` 没有 `content_type` 字段，V2 的 `presign()` 对分片预签名时
  **不**把 `Content-Type` 列入签名；但 Aliyun OSS 收到带 `Content-Type` 的请求时仍会
  纳入 V4 校验，导致 `SignatureDoesNotMatch`(403)。实测结论：分片直传 PUT **绝不能**带
  `Content-Type` 头（浏览器用空类型 Blob 规避）。最终对象的 `Content-Type` 在
  `init_multipart` 时已设定，与分片无关。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote

import alibabacloud_oss_v2 as oss
from fastapi.concurrency import run_in_threadpool

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigError, NotFoundError, StorageError
from app.services.sts import STSClient

logger = logging.getLogger("app.oss")

# 分片直传统一 Content-Type（见模块 docstring 说明）。
PART_CONTENT_TYPE = "application/octet-stream"


@dataclass
class RemotePart:
    part_number: int
    etag: str
    size: int


@dataclass
class ObjectMeta:
    key: str
    size: int
    etag: str
    content_type: Optional[str] = None


class OSSService:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._client: Optional[oss.Client] = None
        self._sign_client: Optional[oss.Client] = None
        self._sts: Optional[STSClient] = None

    # ------------------------------------------------------------------
    # STS 接入
    # ------------------------------------------------------------------
    def _get_sts(self) -> STSClient:
        if self._sts is None:
            self._sts = STSClient(self._settings)
        return self._sts

    def _make_credentials_provider(self):
        """构造 OSS 客户端的凭证提供器。

        - 开启 STS：返回自动刷新的临时凭证提供器（长期密钥不下发 OSS 客户端）。
        - 否则：使用长期 AccessKey 的静态凭证。
        """
        s = self._settings
        if s.oss_use_sts:
            sts = self._get_sts()
            # CredentialsProviderFunc 会在每次 OSS 调用时调用该函数，
            # 临近过期时由 STSClient 自动刷新临时凭证。
            return oss.credentials.CredentialsProviderFunc(sts.as_oss_credentials)
        if not s.oss_access_key_id or not s.oss_access_key_secret:
            raise ConfigError("OSS AccessKey 未配置")
        return oss.credentials.StaticCredentialsProvider(
            s.oss_access_key_id, s.oss_access_key_secret
        )

    # ------------------------------------------------------------------
    # 客户端构建
    # ------------------------------------------------------------------
    def _build_config(self, endpoint: str, *, use_cname: bool = False) -> oss.config.Config:
        s = self._settings
        if not endpoint:
            raise ConfigError("OSS endpoint 未配置")
        cfg = oss.config.load_default()
        cfg.region = s.oss_region or ""
        cfg.credentials_provider = self._make_credentials_provider()
        cfg.endpoint = endpoint
        cfg.use_cname = use_cname
        sv = (s.oss_signature_version or "v4").lower()
        if sv in ("v1", "v2", "v4"):
            cfg.signature_version = sv
        cfg.connect_timeout = 20
        cfg.readwrite_timeout = 60
        return cfg

    @property
    def client(self) -> oss.Client:
        """服务端调用 OSS 用，走内网 endpoint（若配置了内网地址）。"""
        if self._client is None:
            self._client = oss.Client(self._build_config(self._settings.oss_write_endpoint))
        return self._client

    @property
    def sign_client(self) -> oss.Client:
        """生成给浏览器签名的客户端，走公网/CDN endpoint。"""
        if self._sign_client is None:
            s = self._settings
            use_cname = bool(s.oss_cdn_domain) and s.oss_use_cname
            self._sign_client = oss.Client(self._build_config(s.oss_public_endpoint, use_cname=use_cname))
        return self._sign_client

    # ------------------------------------------------------------------
    # 错误转换
    # ------------------------------------------------------------------
    @staticmethod
    def _unwrap(exc: Exception) -> Exception:
        """V2 客户端会把真实 ServiceError 包进 OperationError（存于 `._error`）。

        不解包的话，404/NoSuchKey 等可识别错误会被当成通用 StorageError，
        导致 head_object 在对象不存在时抛错而非返回 None。
        """
        if isinstance(exc, oss.exceptions.OperationError):
            inner = getattr(exc, "_error", None)
            if isinstance(inner, oss.exceptions.ServiceError):
                return inner
        return exc

    @staticmethod
    def _wrap(exc: Exception, action: str) -> Exception:
        exc = OSSService._unwrap(exc)
        if isinstance(exc, oss.exceptions.ServiceError):
            # V2 SDK 的语义错误码在 .code（如 NoSuchKey）；.ec 是数字 EC，仅作参考。
            oss_code = getattr(exc, "code", None) or getattr(exc, "ec", None)
            if oss_code in ("NoSuchUpload", "NoSuchKey", "NoSuchObject"):
                return NotFoundError("对象或分片上传任务不存在/已过期", code="oss_not_found")
            msg = getattr(exc, "message", None) or str(exc)
            return StorageError(f"OSS {action} 失败: {oss_code}", details=msg)
        if isinstance(exc, oss.exceptions.BaseError):
            return StorageError(f"OSS {action} 异常: {exc}")
        logger.exception("OSS %s 异常", action)
        return StorageError(f"OSS {action} 异常: {exc}")

    # ------------------------------------------------------------------
    # 分片上传
    # ------------------------------------------------------------------
    async def init_multipart(
        self,
        key: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """创建分片上传任务，返回 OSS uploadId。

        最终对象的 Content-Type 由这里的 header 决定，而不是各个分片。
        """
        enc_meta = None
        if metadata:
            # 非 ASCII（如中文文件名）需 URL 编码后再写入自定义元数据头
            enc_meta = {k: quote(str(v), safe="") for k, v in metadata.items()}

        def _call() -> str:
            req = oss.InitiateMultipartUploadRequest(
                bucket=self._settings.oss_bucket,
                key=key,
                content_type=content_type or "application/octet-stream",
                metadata=enc_meta,
            )
            return self.client.initiate_multipart_upload(req).upload_id

        try:
            return await run_in_threadpool(_call)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "初始化分片上传") from exc

    async def presign_part(self, key: str, upload_id: str, part_number: int, expires: int) -> str:
        """签发单个分片的 PUT 直传地址（URL 中已含 partNumber / uploadId）。"""

        def _call() -> str:
            req = oss.UploadPartRequest(
                bucket=self._settings.oss_bucket,
                key=key,
                upload_id=upload_id,
                part_number=part_number,
            )
            return self.sign_client.presign(req, expires=timedelta(seconds=expires)).url

        try:
            return await run_in_threadpool(_call)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "签发分片上传地址") from exc

    async def presign_put(
        self, key: str, expires: int, content_type: str = PART_CONTENT_TYPE
    ) -> str:
        """签发整体 PUT 直传地址（小文件用）。"""

        def _call() -> str:
            req = oss.PutObjectRequest(bucket=self._settings.oss_bucket, key=key)
            return self.sign_client.presign(req, expires=timedelta(seconds=expires)).url

        try:
            return await run_in_threadpool(_call)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "签发上传地址") from exc

    async def upload_part(
        self, key: str, upload_id: str, part_number: int, data: bytes
    ) -> str:
        """服务端代理上传分片（降级通道），返回 ETag。"""

        def _call() -> str:
            req = oss.UploadPartRequest(
                bucket=self._settings.oss_bucket,
                key=key,
                upload_id=upload_id,
                part_number=part_number,
                body=data,
            )
            return self.client.upload_part(req).etag

        try:
            return await run_in_threadpool(_call)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "上传分片") from exc

    async def list_parts(self, key: str, upload_id: str) -> List[RemotePart]:
        """列举 OSS 端已落盘的分片，这是上传进度的唯一可信来源。"""

        def _call() -> List[RemotePart]:
            parts: List[RemotePart] = []
            marker = 0
            while True:
                req = oss.ListPartsRequest(
                    bucket=self._settings.oss_bucket,
                    key=key,
                    upload_id=upload_id,
                    part_number_marker=marker,
                    max_parts=1000,
                )
                res = self.client.list_parts(req)
                for p in res.parts or []:
                    parts.append(
                        RemotePart(
                            part_number=p.part_number,
                            etag=(p.etag or "").strip('"'),
                            size=int(p.size or 0),
                        )
                    )
                if not res.is_truncated:
                    break
                marker = res.next_part_number_marker or 0
                if not marker:
                    break
            return sorted(parts, key=lambda p: p.part_number)

        try:
            return await run_in_threadpool(_call)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "列举分片") from exc

    async def complete_multipart(
        self, key: str, upload_id: str, parts: Sequence[RemotePart]
    ) -> ObjectMeta:
        total = sum(p.size for p in parts)

        def _call() -> str:
            upload_parts = [
                oss.UploadPart(part_number=p.part_number, etag=p.etag)
                for p in sorted(parts, key=lambda x: x.part_number)
            ]
            req = oss.CompleteMultipartUploadRequest(
                bucket=self._settings.oss_bucket,
                key=key,
                upload_id=upload_id,
                complete_multipart_upload=oss.CompleteMultipartUpload(parts=upload_parts),
            )
            return (self.client.complete_multipart_upload(req).etag or "").strip('"')

        try:
            etag = await run_in_threadpool(_call)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "合并分片") from exc
        logger.info("合并分片完成 key=%s parts=%d size=%d", key, len(parts), total)
        return ObjectMeta(key=key, size=total, etag=etag)

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        def _call() -> None:
            req = oss.AbortMultipartUploadRequest(
                bucket=self._settings.oss_bucket, key=key, upload_id=upload_id
            )
            self.client.abort_multipart_upload(req)

        try:
            await run_in_threadpool(_call)
        except NotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            wrapped = self._wrap(exc, "取消分片上传")
            if isinstance(wrapped, NotFoundError):
                return
            raise wrapped from exc

    # ------------------------------------------------------------------
    # 普通对象操作
    # ------------------------------------------------------------------
    async def put_object(
        self, key: str, data: bytes, content_type: Optional[str] = None
    ) -> ObjectMeta:
        def _call() -> str:
            req = oss.PutObjectRequest(
                bucket=self._settings.oss_bucket,
                key=key,
                body=data,
                content_type=content_type or "application/octet-stream",
            )
            return (self.client.put_object(req).etag or "").strip('"')

        try:
            etag = await run_in_threadpool(_call)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "上传对象") from exc
        return ObjectMeta(key=key, size=len(data), etag=etag, content_type=content_type)

    async def head_object(self, key: str) -> Optional[ObjectMeta]:
        def _call():
            req = oss.HeadObjectRequest(bucket=self._settings.oss_bucket, key=key)
            return self.client.head_object(req)

        try:
            res = await run_in_threadpool(_call)
        except NotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001
            wrapped = self._wrap(exc, "查询对象")
            if isinstance(wrapped, NotFoundError):
                return None
            raise wrapped from exc

        return ObjectMeta(
            key=key,
            size=int(res.content_length or 0),
            etag=(res.etag or "").strip('"'),
            content_type=res.content_type,
        )

    def download_to_file_sync(self, key: str, filepath: str) -> int:
        """把对象下载到本地文件，返回字节数。**同步方法，供 Celery worker 调用。**

        本类其余方法都是 async（配合 FastAPI），但 Celery worker 是同步进程，
        在里面起 event loop 只为了 await 一次下载得不偿失。SDK 本身就是同步的，
        异步方法不过是套了层 run_in_threadpool，这里直接调裸接口即可。

        用 ``get_object_to_file`` 而非先读进内存：DM 手册动辄上百 MB，
        全量读进内存再落盘会让 worker 的常驻内存直接翻倍。
        """
        import os

        def _call() -> int:
            req = oss.GetObjectRequest(bucket=self._settings.oss_bucket, key=key)
            self.client.get_object_to_file(req, filepath)
            return os.path.getsize(filepath)

        try:
            return _call()
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "下载对象") from exc

    async def sign_download_url(
        self,
        key: str,
        expires: int,
        filename: Optional[str] = None,
        inline: bool = False,
    ) -> str:
        def _call() -> str:
            disposition = None
            if filename:
                kind = "inline" if inline else "attachment"
                encoded = quote(filename, safe="")
                disposition = (
                    f'{kind}; filename="{encoded}"; filename*=UTF-8\'\'{encoded}'
                )
            req = oss.GetObjectRequest(
                bucket=self._settings.oss_bucket,
                key=key,
                response_content_disposition=disposition,
            )
            return self.sign_client.presign(req, expires=timedelta(seconds=expires)).url

        try:
            return await run_in_threadpool(_call)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "签发下载地址") from exc

    async def delete_object(self, key: str) -> None:
        def _call() -> None:
            req = oss.DeleteObjectRequest(bucket=self._settings.oss_bucket, key=key)
            self.client.delete_object(req)

        try:
            await run_in_threadpool(_call)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "删除对象") from exc

    async def ping(self) -> bool:
        """健康检查：确认 bucket 可访问。"""

        def _call() -> bool:
            self.client.get_bucket_info(
                oss.GetBucketInfoRequest(bucket=self._settings.oss_bucket)
            )
            return True

        try:
            return await run_in_threadpool(_call)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OSS 连通性检查失败: %s", exc)
            return False


_oss_service: Optional[OSSService] = None


def get_oss_service() -> OSSService:
    global _oss_service
    if _oss_service is None:
        _oss_service = OSSService()
    return _oss_service
