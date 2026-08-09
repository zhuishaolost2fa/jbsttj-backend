"""分片上传接口。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status

from app.core.exceptions import ValidationError
from app.core.security import CurrentUser, get_current_user
from app.schemas.common import MessageResponse, PageResult, Pagination
from app.schemas.upload import (
    BatchPartCallbackRequest,
    CompleteUploadRequest,
    CompleteUploadResponse,
    InitUploadRequest,
    InitUploadResponse,
    PartCallbackRequest,
    PresignPartsRequest,
    PresignPartsResponse,
    TaskBrief,
    TaskStatusResponse,
    UploadedPart,
)
from app.services.upload_service import UploadService, get_upload_service

router = APIRouter(prefix="/uploads", tags=["分片上传"])

# 代理上传单片的体积上限，防止内存被打爆
MAX_PROXY_PART_BYTES = 32 * 1024 * 1024


@router.post(
    "/init",
    response_model=InitUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="初始化分片上传",
    description=(
        "创建上传任务并返回分片规格。三种可能的结果：\n\n"
        "- `instant=true`：命中秒传，文件已存在，前端无需上传任何数据；\n"
        "- `resumed=true`：命中断点续传，`uploaded_parts` 中的分片可直接跳过；\n"
        "- 两者均为 false：全新任务，需要上传全部 `total_parts` 个分片。\n\n"
        "前端必须严格按返回的 `chunk_size` 切片，服务端会根据文件大小自动调整该值。"
    ),
)
async def init_upload(
    payload: InitUploadRequest,
    user: CurrentUser = Depends(get_current_user),
    service: UploadService = Depends(get_upload_service),
) -> InitUploadResponse:
    return await service.init_upload(user, payload)


@router.post(
    "/temp/init",
    response_model=InitUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="初始化临时文件分片上传",
    description=(
        "与 `/init` 等价，但对象固定写入 `temp/` 前缀，便于用 OSS 生命周期规则设置过期自动清理。\n\n"
        "秒传 / 断点续传的匹配也限定在 `temp/` 命名空间内，不会复用永久文件对象"
        "（避免临时对象被生命周期删除后拖垮永久文件）。"
    ),
)
async def init_temp_upload(
    payload: InitUploadRequest,
    user: CurrentUser = Depends(get_current_user),
    service: UploadService = Depends(get_upload_service),
) -> InitUploadResponse:
    payload.upload_type = "temporary"
    return await service.init_upload(user, payload)


@router.post(
    "/{task_id}/presign",
    response_model=PresignPartsResponse,
    summary="批量签发分片直传地址",
    description=(
        "返回若干个分片的预签名 PUT 地址，前端直接 PUT 到 OSS，数据不经过本服务。\n\n"
        "**重要**：PUT 时**不要**设置 `Content-Type`。V2 SDK 的预签名不对该头签名，"
        "一旦请求带上任何 `Content-Type`（浏览器会自动附带），OSS 会返回 "
        "`SignatureDoesNotMatch`(403)。前端已用空类型 Blob 规避；最终对象的 "
        "`Content-Type` 在 init 时已确定。"
    ),
)
async def presign_parts(
    payload: PresignPartsRequest,
    task_id: str = Path(description="上传任务 ID"),
    user: CurrentUser = Depends(get_current_user),
    service: UploadService = Depends(get_upload_service),
) -> PresignPartsResponse:
    return await service.presign_parts(user, task_id, payload)


@router.post(
    "/{task_id}/parts/{part_number}/callback",
    response_model=UploadedPart,
    summary="上报单个分片上传结果",
    description="前端直传成功后回调本接口，把 OSS 返回的 ETag 记录到数据库，便于进度展示与审计。",
)
async def report_part(
    payload: PartCallbackRequest,
    task_id: str = Path(description="上传任务 ID"),
    part_number: int = Path(ge=1, le=10000, description="分片序号，从 1 开始"),
    user: CurrentUser = Depends(get_current_user),
    service: UploadService = Depends(get_upload_service),
) -> UploadedPart:
    return await service.record_part(user, task_id, part_number, payload)


@router.post(
    "/{task_id}/parts/callback",
    response_model=MessageResponse,
    summary="批量上报分片上传结果",
    description="并发上传场景下推荐批量上报，减少请求数。",
)
async def report_parts(
    payload: BatchPartCallbackRequest,
    task_id: str = Path(description="上传任务 ID"),
    user: CurrentUser = Depends(get_current_user),
    service: UploadService = Depends(get_upload_service),
) -> MessageResponse:
    count = await service.record_parts(user, task_id, payload)
    return MessageResponse(message=f"已记录 {count} 个分片")


@router.put(
    "/{task_id}/parts/{part_number}",
    response_model=UploadedPart,
    summary="服务端代理上传分片（降级方案）",
    description=(
        "请求体为分片的原始二进制内容（`Content-Type: application/octet-stream`）。\n\n"
        "仅在浏览器无法直连 OSS 时使用，会占用应用服务器带宽。"
    ),
)
async def proxy_upload_part(
    request: Request,
    task_id: str = Path(description="上传任务 ID"),
    part_number: int = Path(ge=1, le=10000, description="分片序号，从 1 开始"),
    user: CurrentUser = Depends(get_current_user),
    service: UploadService = Depends(get_upload_service),
) -> UploadedPart:
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_PROXY_PART_BYTES:
        raise ValidationError(
            f"单个分片不能超过 {MAX_PROXY_PART_BYTES // 1024 // 1024}MB",
            code="part_too_large",
        )
    data = await request.body()
    if len(data) > MAX_PROXY_PART_BYTES:
        raise ValidationError("分片体积超出限制", code="part_too_large")
    return await service.proxy_upload_part(user, task_id, part_number, data)


@router.get(
    "",
    response_model=PageResult[TaskBrief],
    summary="查询我的上传任务列表",
)
async def list_tasks(
    task_status: Optional[str] = Query(
        default=None,
        alias="status",
        pattern="^(uploading|completed|aborted|failed)$",
        description="按状态过滤",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(get_current_user),
    service: UploadService = Depends(get_upload_service),
) -> PageResult[TaskBrief]:
    items, total = await service.list_tasks(
        user, status=task_status, limit=limit, offset=offset
    )
    return PageResult[TaskBrief](
        items=items,
        pagination=Pagination(
            total=total, limit=limit, offset=offset, has_more=offset + len(items) < total
        ),
    )


@router.get(
    "/{task_id}",
    response_model=TaskStatusResponse,
    summary="查询上传进度",
    description="返回 OSS 端实际已落盘的分片列表，断网重连后据此续传。",
)
async def get_task_status(
    task_id: str = Path(description="上传任务 ID"),
    user: CurrentUser = Depends(get_current_user),
    service: UploadService = Depends(get_upload_service),
) -> TaskStatusResponse:
    return await service.get_status(user, task_id)


@router.post(
    "/{task_id}/complete",
    response_model=CompleteUploadResponse,
    summary="合并分片完成上传",
    description=(
        "服务端会重新向 OSS 列举分片做完整性校验，缺片会返回 409 并给出缺失的分片号。\n"
        "接口幂等，重复调用返回同一结果。"
    ),
)
async def complete_upload(
    payload: Optional[CompleteUploadRequest] = None,
    task_id: str = Path(description="上传任务 ID"),
    user: CurrentUser = Depends(get_current_user),
    service: UploadService = Depends(get_upload_service),
) -> CompleteUploadResponse:
    return await service.complete_upload(user, task_id, payload or CompleteUploadRequest())


@router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="取消上传任务",
    description="同时清理 OSS 上已产生的分片碎片，避免产生存储费用。",
)
async def abort_upload(
    task_id: str = Path(description="上传任务 ID"),
    user: CurrentUser = Depends(get_current_user),
    service: UploadService = Depends(get_upload_service),
) -> Response:
    await service.abort_upload(user, task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
