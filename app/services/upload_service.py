"""分片上传业务编排。

设计要点：
1. **OSS 是进度的唯一可信来源。** 数据库里的分片记录只是加速查询用的缓存，
   合并前一定重新 list_parts，避免前端漏报/错报 ETag 导致合并出脏数据。
2. **秒传** 依赖客户端提供的内容指纹 file_hash：命中即复用同一个 OSS 对象，
   只新增一条 files 记录（多条记录可指向同一 object_key，删除时按引用计数处理）。
3. **断点续传** 复用同一个 uploadId；若 OSS 端任务已过期失效，自动降级为重新初始化。
4. 所有查询都带 user_id 过滤，杜绝越权访问他人任务。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import Settings, get_settings
from app.core.exceptions import ConflictError, NotFoundError, StorageError, ValidationError
from app.core.security import CurrentUser
from app.schemas.file import FileInfo
from app.schemas.upload import (
    BatchPartCallbackRequest,
    CompleteUploadRequest,
    CompleteUploadResponse,
    InitUploadRequest,
    InitUploadResponse,
    PartCallbackRequest,
    PresignedPart,
    PresignPartsRequest,
    PresignPartsResponse,
    TaskBrief,
    TaskStatusResponse,
    UploadedPart,
)
from app.services.oss import PART_CONTENT_TYPE, OSSService, RemotePart, get_oss_service
from app.services.repository import (
    FileRepository,
    UploadPartRepository,
    UploadTaskRepository,
)
from app.utils.files import (
    build_object_key,
    calc_total_parts,
    guess_content_type,
    resolve_chunk_size,
    sanitize_filename,
    validate_extension,
    validate_file_size,
)

logger = logging.getLogger("app.upload")

STATUS_UPLOADING = "uploading"
STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"
STATUS_FAILED = "failed"


class UploadService:
    def __init__(
        self,
        oss: Optional[OSSService] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.oss = oss or get_oss_service()
        self.settings = settings or get_settings()
        self.tasks = UploadTaskRepository()
        self.parts = UploadPartRepository()
        self.files = FileRepository()

    # ==================================================================
    # 1. 初始化
    # ==================================================================
    async def init_upload(self, user: CurrentUser, req: InitUploadRequest) -> InitUploadResponse:
        filename = sanitize_filename(req.filename)
        validate_extension(filename, self.settings)
        validate_file_size(req.file_size, self.settings)

        content_type = guess_content_type(filename, req.content_type)
        chunk_size = resolve_chunk_size(req.file_size, req.chunk_size, self.settings)
        total_parts = calc_total_parts(req.file_size, chunk_size)

        # 临时文件走独立前缀，便于 OSS 生命周期规则专门清理，
        # 且与永久文件命名空间隔离（秒传/续传也限定在同一前缀内）。
        prefix = (
            self.settings.temp_upload_prefix
            if req.upload_type == "temporary"
            else self.settings.upload_prefix
        )

        # ---- 秒传 ----
        if req.file_hash:
            instant = await self._try_instant_upload(user, req, filename, content_type, prefix)
            if instant:
                return instant

        # ---- 断点续传 ----
        if req.file_hash:
            resumed = await self._try_resume(user, req, chunk_size, total_parts, prefix)
            if resumed:
                return resumed

        # ---- 全新任务 ----
        object_key = build_object_key(user.id, filename, prefix)
        upload_id = await self.oss.init_multipart(
            object_key,
            content_type=content_type,
            metadata={"user-id": user.id, "origin-name": filename},
        )

        try:
            task = await self.tasks.create(
                {
                    "user_id": user.id,
                    "bucket": self.settings.oss_bucket,
                    "object_key": object_key,
                    "filename": filename,
                    "content_type": content_type,
                    "file_size": req.file_size,
                    "chunk_size": chunk_size,
                    "total_parts": total_parts,
                    "upload_id": upload_id,
                    "file_hash": req.file_hash,
                    "status": STATUS_UPLOADING,
                    "metadata": {**(req.metadata or {}), "upload_type": req.upload_type},
                    "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
                }
            )
        except Exception:
            # 落库失败要回滚 OSS 侧的分片任务，否则会留下永久碎片产生存储费用
            await self._safe_abort(object_key, upload_id)
            raise

        logger.info(
            "创建上传任务 task=%s user=%s size=%s parts=%s type=%s",
            task["id"], user.id, req.file_size, total_parts, req.upload_type,
        )
        return InitUploadResponse(
            task_id=str(task["id"]),
            object_key=object_key,
            upload_id=upload_id,
            chunk_size=chunk_size,
            total_parts=total_parts,
            file_size=req.file_size,
            status=STATUS_UPLOADING,
            upload_type=req.upload_type,
            uploaded_parts=[],
            part_content_type=PART_CONTENT_TYPE,
        )

    async def _try_instant_upload(
        self, user: CurrentUser, req: InitUploadRequest, filename: str, content_type: str, prefix: str
    ) -> Optional[InitUploadResponse]:
        existing = await self.files.find_by_hash(user.id, req.file_hash or "", key_prefix=prefix)
        if not existing or int(existing.get("file_size") or 0) != req.file_size:
            return None

        # 数据库有记录不代表对象还在，删除/生命周期规则都可能让它消失
        meta = await self.oss.head_object(existing["object_key"])
        if not meta:
            logger.warning("秒传命中但对象已不存在: %s", existing["object_key"])
            return None

        task = await self.tasks.create(
            {
                "user_id": user.id,
                "bucket": existing["bucket"],
                "object_key": existing["object_key"],
                "filename": filename,
                "content_type": content_type,
                "file_size": req.file_size,
                "chunk_size": self.settings.upload_chunk_size,
                "total_parts": 0,
                "file_hash": req.file_hash,
                "status": STATUS_COMPLETED,
                "metadata": req.metadata or {},
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        file_row = await self.files.create(
            {
                "user_id": user.id,
                "task_id": task["id"],
                "bucket": existing["bucket"],
                "object_key": existing["object_key"],
                "filename": filename,
                "content_type": content_type,
                "file_size": req.file_size,
                "file_hash": req.file_hash,
                "etag": existing.get("etag") or meta.etag,
                "metadata": req.metadata or {},
            }
        )
        logger.info("秒传命中 user=%s key=%s", user.id, existing["object_key"])
        return InitUploadResponse(
            task_id=str(task["id"]),
            object_key=existing["object_key"],
            upload_id=None,
            chunk_size=self.settings.upload_chunk_size,
            total_parts=0,
            file_size=req.file_size,
            status=STATUS_COMPLETED,
            instant=True,
            upload_type=req.upload_type,
            file=FileInfo.from_row(file_row),
        )

    async def _try_resume(
        self, user: CurrentUser, req: InitUploadRequest, chunk_size: int, total_parts: int, prefix: str
    ) -> Optional[InitUploadResponse]:
        task = await self.tasks.find_resumable(
            user.id, req.file_hash or "", req.file_size, key_prefix=prefix
        )
        if not task or not task.get("upload_id"):
            return None

        # 分片大小变了就没法续传，必须重新来过
        if int(task.get("chunk_size") or 0) != chunk_size:
            return None

        try:
            remote_parts = await self.oss.list_parts(task["object_key"], task["upload_id"])
        except NotFoundError:
            logger.info("原 uploadId 已失效，转为新建任务 task=%s", task["id"])
            await self.tasks.mark_failed(str(task["id"]), "OSS uploadId 已过期")
            return None
        except StorageError as exc:
            logger.warning("续传检查失败，转为新建任务: %s", exc)
            return None

        await self.parts.record_many(
            str(task["id"]),
            [{"part_number": p.part_number, "etag": p.etag, "size": p.size} for p in remote_parts],
        )
        logger.info("断点续传 task=%s 已上传 %d/%d 片", task["id"], len(remote_parts), total_parts)
        return InitUploadResponse(
            task_id=str(task["id"]),
            object_key=task["object_key"],
            upload_id=task["upload_id"],
            chunk_size=chunk_size,
            total_parts=int(task.get("total_parts") or total_parts),
            file_size=req.file_size,
            status=STATUS_UPLOADING,
            resumed=True,
            upload_type=req.upload_type,
            uploaded_parts=[
                UploadedPart(part_number=p.part_number, etag=p.etag, size=p.size)
                for p in remote_parts
            ],
            part_content_type=PART_CONTENT_TYPE,
        )

    # ==================================================================
    # 2. 分片签名
    # ==================================================================
    async def presign_parts(
        self, user: CurrentUser, task_id: str, req: PresignPartsRequest
    ) -> PresignPartsResponse:
        task = await self._get_active_task(user, task_id)
        total_parts = int(task["total_parts"])

        if len(req.part_numbers) > self.settings.max_presign_batch:
            raise ValidationError(
                f"单次最多签发 {self.settings.max_presign_batch} 个分片地址",
                code="too_many_parts",
            )
        invalid = [n for n in req.part_numbers if n > total_parts]
        if invalid:
            raise ValidationError(f"分片序号超出范围 1..{total_parts}: {invalid[:5]}")

        expires = req.expires or self.settings.presign_expire_seconds
        parts: List[PresignedPart] = []
        for number in req.part_numbers:
            url = await self.oss.presign_part(task["object_key"], task["upload_id"], number, expires)
            parts.append(PresignedPart(part_number=number, url=url))

        return PresignPartsResponse(
            task_id=task_id,
            object_key=task["object_key"],
            upload_id=task["upload_id"],
            expires_in=expires,
            part_content_type=PART_CONTENT_TYPE,
            parts=parts,
        )

    # ==================================================================
    # 3. 分片上报 / 代理上传
    # ==================================================================
    async def record_part(
        self, user: CurrentUser, task_id: str, part_number: int, req: PartCallbackRequest
    ) -> UploadedPart:
        task = await self._get_active_task(user, task_id)
        if part_number < 1 or part_number > int(task["total_parts"]):
            raise ValidationError(f"分片序号必须在 1..{task['total_parts']} 之间")
        await self.parts.record(task_id, part_number, req.etag, req.size)
        return UploadedPart(part_number=part_number, etag=req.etag.strip('"'), size=req.size)

    async def record_parts(
        self, user: CurrentUser, task_id: str, req: BatchPartCallbackRequest
    ) -> int:
        task = await self._get_active_task(user, task_id)
        total = int(task["total_parts"])
        for p in req.parts:
            if p.part_number < 1 or p.part_number > total:
                raise ValidationError(f"分片序号 {p.part_number} 超出范围 1..{total}")
        await self.parts.record_many(
            task_id,
            [{"part_number": p.part_number, "etag": p.etag, "size": p.size} for p in req.parts],
        )
        return len(req.parts)

    async def proxy_upload_part(
        self, user: CurrentUser, task_id: str, part_number: int, data: bytes
    ) -> UploadedPart:
        """降级通道：分片先到后端再转发 OSS。

        适用于浏览器无法直连 OSS（内网隔离、OSS 未开 CORS）的场景，
        代价是占用应用服务器带宽，能用直传就别用它。
        """
        task = await self._get_active_task(user, task_id)
        total_parts = int(task["total_parts"])
        if part_number < 1 or part_number > total_parts:
            raise ValidationError(f"分片序号必须在 1..{total_parts} 之间")
        if not data:
            raise ValidationError("分片内容为空")

        chunk_size = int(task["chunk_size"])
        if part_number < total_parts and len(data) != chunk_size:
            raise ValidationError(
                f"第 {part_number} 片大小应为 {chunk_size} 字节，实际 {len(data)} 字节",
                code="part_size_mismatch",
            )

        etag = await self.oss.upload_part(task["object_key"], task["upload_id"], part_number, data)
        await self.parts.record(task_id, part_number, etag, len(data))
        return UploadedPart(part_number=part_number, etag=etag.strip('"'), size=len(data))

    # ==================================================================
    # 4. 进度查询
    # ==================================================================
    async def get_status(self, user: CurrentUser, task_id: str) -> TaskStatusResponse:
        task = await self._get_task(user, task_id)
        uploaded: List[UploadedPart] = []

        if task["status"] == STATUS_UPLOADING and task.get("upload_id"):
            try:
                remote = await self.oss.list_parts(task["object_key"], task["upload_id"])
                uploaded = [
                    UploadedPart(part_number=p.part_number, etag=p.etag, size=p.size)
                    for p in remote
                ]
            except (NotFoundError, StorageError):
                rows = await self.parts.list_by_task(task_id)
                uploaded = [
                    UploadedPart(
                        part_number=r["part_number"], etag=r["etag"], size=int(r.get("size") or 0)
                    )
                    for r in rows
                ]
        elif task["status"] != STATUS_UPLOADING:
            rows = await self.parts.list_by_task(task_id)
            uploaded = [
                UploadedPart(part_number=r["part_number"], etag=r["etag"], size=int(r.get("size") or 0))
                for r in rows
            ]

        file_size = int(task["file_size"])
        uploaded_bytes = sum(p.size for p in uploaded)
        if task["status"] == STATUS_COMPLETED:
            uploaded_bytes, progress = file_size, 100.0
        else:
            progress = round(min(uploaded_bytes / file_size * 100, 100), 2) if file_size else 0.0

        return TaskStatusResponse(
            task_id=str(task["id"]),
            object_key=task["object_key"],
            upload_id=task.get("upload_id"),
            filename=task["filename"],
            file_size=file_size,
            chunk_size=int(task["chunk_size"]),
            total_parts=int(task["total_parts"]),
            status=task["status"],
            uploaded_parts=uploaded,
            uploaded_bytes=uploaded_bytes,
            progress=progress,
            error_message=task.get("error_message"),
            created_at=task.get("created_at"),
            updated_at=task.get("updated_at"),
        )

    async def list_tasks(
        self, user: CurrentUser, *, status: Optional[str], limit: int, offset: int
    ) -> Tuple[List[TaskBrief], int]:
        rows, total = await self.tasks.list_by_user(
            user.id, status=status, limit=limit, offset=offset
        )
        items = [
            TaskBrief(
                task_id=str(r["id"]),
                filename=r["filename"],
                file_size=int(r["file_size"]),
                total_parts=int(r["total_parts"]),
                status=r["status"],
                created_at=r.get("created_at"),
            )
            for r in rows
        ]
        return items, total

    # ==================================================================
    # 5. 合并 / 取消
    # ==================================================================
    async def complete_upload(
        self, user: CurrentUser, task_id: str, req: CompleteUploadRequest
    ) -> CompleteUploadResponse:
        task = await self._get_task(user, task_id)

        # 幂等：重复调用直接返回已有结果，避免前端重试时报错
        if task["status"] == STATUS_COMPLETED:
            existing = await self.files.get_by_task(task_id, user.id)
            if existing:
                return CompleteUploadResponse(
                    task_id=task_id, file=FileInfo.from_row(existing), message="任务已完成"
                )

        if task["status"] in {STATUS_ABORTED, STATUS_FAILED}:
            raise ConflictError(f"任务状态为 {task['status']}，无法合并")
        if not task.get("upload_id"):
            raise ConflictError("该任务没有分片上传上下文")

        remote_parts = await self.oss.list_parts(task["object_key"], task["upload_id"])
        if not remote_parts:
            raise ConflictError("OSS 上没有任何已完成的分片", code="no_parts")

        total_parts = int(task["total_parts"])
        got = {p.part_number for p in remote_parts}
        missing = sorted(set(range(1, total_parts + 1)) - got)
        if missing:
            raise ConflictError(
                f"还有 {len(missing)} 个分片未上传完成",
                code="incomplete_parts",
                details={"missing_parts": missing[:50], "missing_count": len(missing)},
            )

        # 前端上报的 ETag 只用于交叉校验，真正合并用 OSS 侧数据
        if req.parts:
            client_map = {p.part_number: p.etag.strip('"') for p in req.parts}
            mismatched = [
                p.part_number
                for p in remote_parts
                if p.part_number in client_map and client_map[p.part_number] != p.etag.strip('"')
            ]
            if mismatched:
                raise ConflictError(
                    "部分分片校验值与服务端不一致，请重传这些分片",
                    code="etag_mismatch",
                    details={"parts": mismatched[:20]},
                )

        merged_size = sum(p.size for p in remote_parts)
        if req.verify_size and merged_size != int(task["file_size"]):
            raise ConflictError(
                f"分片总大小 {merged_size} 与声明的 {task['file_size']} 不一致",
                code="size_mismatch",
            )

        try:
            meta = await self.oss.complete_multipart(
                task["object_key"], task["upload_id"], remote_parts
            )
        except Exception as exc:  # noqa: BLE001
            await self.tasks.mark_failed(task_id, str(exc))
            raise

        await self.parts.record_many(
            task_id,
            [{"part_number": p.part_number, "etag": p.etag, "size": p.size} for p in remote_parts],
        )

        file_row = await self.files.create(
            {
                "user_id": user.id,
                "task_id": task_id,
                "bucket": task["bucket"],
                "object_key": task["object_key"],
                "filename": task["filename"],
                "content_type": task.get("content_type"),
                "file_size": merged_size,
                "file_hash": task.get("file_hash"),
                "etag": meta.etag,
                "metadata": task.get("metadata") or {},
            }
        )
        await self.tasks.mark_completed(task_id)
        logger.info("上传完成 task=%s key=%s size=%s", task_id, task["object_key"], merged_size)

        return CompleteUploadResponse(task_id=task_id, file=FileInfo.from_row(file_row))

    async def abort_upload(self, user: CurrentUser, task_id: str) -> None:
        task = await self._get_task(user, task_id)
        if task["status"] == STATUS_COMPLETED:
            raise ConflictError("任务已完成，无法取消")
        if task.get("upload_id"):
            await self._safe_abort(task["object_key"], task["upload_id"])
        await self.parts.delete_by_task(task_id)
        await self.tasks.mark_aborted(task_id)
        logger.info("取消上传任务 task=%s", task_id)

    # ==================================================================
    # 内部工具
    # ==================================================================
    async def _get_task(self, user: CurrentUser, task_id: str) -> Dict[str, Any]:
        task = await self.tasks.get(task_id, user_id=user.id)
        if not task:
            raise NotFoundError("上传任务不存在或无权访问", code="task_not_found")
        return task

    async def _get_active_task(self, user: CurrentUser, task_id: str) -> Dict[str, Any]:
        task = await self._get_task(user, task_id)
        if task["status"] != STATUS_UPLOADING:
            raise ConflictError(f"任务状态为 {task['status']}，不可继续上传分片")
        if not task.get("upload_id"):
            raise ConflictError("该任务缺少分片上传上下文")
        return task

    async def _safe_abort(self, object_key: str, upload_id: str) -> None:
        try:
            await self.oss.abort_multipart(object_key, upload_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("回滚 OSS 分片任务失败 key=%s: %s", object_key, exc)


_service: Optional[UploadService] = None


def get_upload_service() -> UploadService:
    global _service
    if _service is None:
        _service = UploadService()
    return _service
