"""文件管理业务：列表、详情、下载签名、删除、小文件直传。"""

from __future__ import annotations

import hashlib
import logging
from typing import List, Optional, Tuple

from app.core.config import Settings, get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.security import CurrentUser
from app.schemas.file import DownloadUrlResponse, FileInfo
from app.services.oss import OSSService, get_oss_service
from app.services.repository import FileRepository
from app.services.script_service import purge_dm_guide_for_file
from app.utils.files import (
    build_object_key,
    guess_content_type,
    sanitize_filename,
    validate_extension,
    validate_file_size,
)

logger = logging.getLogger("app.file")


class FileService:
    def __init__(
        self,
        oss: Optional[OSSService] = None,
        settings: Optional[Settings] = None,
        files: Optional[FileRepository] = None,
    ) -> None:
        self.oss = oss or get_oss_service()
        self.settings = settings or get_settings()
        self.files = files or FileRepository()

    async def list_files(
        self, user: CurrentUser, *, keyword: Optional[str], limit: int, offset: int
    ) -> Tuple[List[FileInfo], int]:
        rows, total = await self.files.list_by_user(
            user.id, keyword=keyword, limit=limit, offset=offset
        )
        return [FileInfo.from_row(r) for r in rows], total

    async def get_file(self, user: CurrentUser, file_id: str) -> FileInfo:
        row = await self.files.get(file_id, user_id=user.id)
        if not row:
            raise NotFoundError("文件不存在或无权访问", code="file_not_found")
        return FileInfo.from_row(row)

    async def get_download_url(
        self,
        user: CurrentUser,
        file_id: str,
        *,
        expires: Optional[int] = None,
        inline: bool = False,
    ) -> DownloadUrlResponse:
        row = await self.files.get(file_id, user_id=user.id)
        if not row:
            raise NotFoundError("文件不存在或无权访问", code="file_not_found")

        ttl = expires or self.settings.download_url_expire_seconds
        url = await self.oss.sign_download_url(
            row["object_key"], ttl, filename=row["filename"], inline=inline
        )
        return DownloadUrlResponse(
            file_id=file_id, filename=row["filename"], url=url, expires_in=ttl, inline=inline
        )

    async def delete_file(self, user: CurrentUser, file_id: str, *, purge: bool = True) -> None:
        """软删除数据库记录；当没有其它记录引用该对象时才真正删除 OSS 对象。

        秒传会让多条记录共享同一个 object_key，所以物理删除必须先看引用计数。

        删的若是某剧本的 DM 手册，还会**联动清空**该剧本的解析产物
        （jobs / documents / chunks / qa / stories / highlights 全部物理删除，
        剧本行保留、摘掉 ``extra.dmGuide`` 引用）—— 文件记录都没了，
        解析产物留着只会让剧本详情挂一个失效的 DM 问答入口。
        清理是 best-effort：失败只记日志，不影响文件删除本身。
        """
        row = await self.files.get(file_id, user_id=user.id)
        if not row:
            raise NotFoundError("文件不存在或无权访问", code="file_not_found")

        await self.files.soft_delete(file_id, user.id)

        remaining = await self.files.count_references(row["object_key"])
        object_dead = purge and remaining == 0

        # DM 手册联动清理：fileId 直接命中必清；OSS 对象将物理删除时
        # 连 objectKey 引用（秒传共享、旧记录无 fileId）一并作废。
        try:
            await purge_dm_guide_for_file(
                file_id, str(row["object_key"]), object_dead=object_dead
            )
        except Exception as exc:  # noqa: BLE001 - 联动失败不阻塞文件删除
            logger.warning("文件删除联动清理 DM 解析产物失败 file=%s: %s", file_id, exc)

        if not purge:
            return
        if remaining == 0:
            try:
                await self.oss.delete_object(row["object_key"])
                logger.info("物理删除对象 %s", row["object_key"])
            except Exception as exc:  # noqa: BLE001
                # 对象删除失败不影响业务语义，留给清理任务兜底
                logger.warning("删除 OSS 对象失败 %s: %s", row["object_key"], exc)
        else:
            logger.info("对象 %s 仍被 %d 条记录引用，跳过物理删除", row["object_key"], remaining)

    async def simple_upload(
        self,
        user: CurrentUser,
        *,
        filename: str,
        content_type: Optional[str],
        data: bytes,
        max_size: int = 20 * 1024 * 1024,
        prefix: Optional[str] = None,
        acl: Optional[str] = None,
        content_disposition: Optional[str] = None,
    ) -> FileInfo:
        """小文件一次性上传，走服务端中转。大文件请使用分片上传接口。

        ``prefix`` 可覆盖默认的对象 key 前缀（settings.upload_prefix），
        例如头像上传传 ``"avatars"`` 以落到独立的命名空间，便于公开接口按 key 回源、
        也避免污染普通文件列表。
        秒传（find_by_hash）同样按该前缀做命名空间隔离：临时文件不会复用永久对象，
        反之亦然——否则临时对象被生命周期规则删除后会拖垮引用同一对象的永久文件。
        ``acl`` 可选，传入如 ``"public-read"`` 可将对象设为匿名可读（头像直链场景）；
        普通文件上传不传，保持 bucket 默认私有。
        """
        if not data:
            raise ValidationError("文件内容为空")
        if len(data) > max_size:
            raise ValidationError(
                f"该接口仅支持 {max_size // 1024 // 1024}MB 以内的文件，请改用分片上传",
                code="file_too_large",
            )

        clean_name = sanitize_filename(filename)
        validate_extension(clean_name, self.settings)
        validate_file_size(len(data), self.settings)

        mime = guess_content_type(clean_name, content_type)
        file_hash = hashlib.sha256(data).hexdigest()

        existing = await self.files.find_by_hash(user.id, file_hash, key_prefix=prefix)
        if existing and int(existing.get("file_size") or 0) == len(data):
            meta = await self.oss.head_object(existing["object_key"])
            if meta:
                if acl:
                    # 秒传复用已有对象，仍按调用方要求确保 ACL（如头像需 public-read）
                    await self.oss.set_object_acl(existing["object_key"], acl)
                row = await self.files.create(
                    {
                        "user_id": user.id,
                        "bucket": existing["bucket"],
                        "object_key": existing["object_key"],
                        "filename": clean_name,
                        "content_type": mime,
                        "file_size": len(data),
                        "file_hash": file_hash,
                        "etag": existing.get("etag") or meta.etag,
                        "metadata": {"instant": True},
                    }
                )
                return FileInfo.from_row(row)

        object_key = build_object_key(user.id, clean_name, prefix or self.settings.upload_prefix)
        meta = await self.oss.put_object(
            object_key, data, content_type=mime, content_disposition=content_disposition
        )
        if acl:
            await self.oss.set_object_acl(object_key, acl)
        row = await self.files.create(
            {
                "user_id": user.id,
                "bucket": self.settings.oss_bucket,
                "object_key": object_key,
                "filename": clean_name,
                "content_type": mime,
                "file_size": len(data),
                "file_hash": file_hash,
                "etag": meta.etag,
                "metadata": {},
            }
        )
        logger.info("小文件上传完成 user=%s key=%s size=%d", user.id, object_key, len(data))
        return FileInfo.from_row(row)


_file_service: Optional[FileService] = None


def get_file_service() -> FileService:
    global _file_service
    if _file_service is None:
        _file_service = FileService()
    return _file_service
