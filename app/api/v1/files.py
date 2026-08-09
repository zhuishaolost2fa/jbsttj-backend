"""文件管理接口。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Path, Query, Response, UploadFile, status

from app.core.security import CurrentUser, get_current_user
from app.schemas.common import PageResult, Pagination
from app.schemas.file import DownloadUrlResponse, FileInfo, SimpleUploadResponse
from app.services.file_service import FileService, get_file_service

router = APIRouter(prefix="/files", tags=["文件管理"])

SIMPLE_UPLOAD_LIMIT = 20 * 1024 * 1024


@router.post(
    "/simple-upload",
    response_model=SimpleUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="小文件直接上传",
    description=(
        f"multipart/form-data 一次性上传，限 {SIMPLE_UPLOAD_LIMIT // 1024 // 1024}MB。"
        "内容相同的文件会自动秒传复用。大文件请走分片上传流程。"
    ),
)
async def simple_upload(
    file: UploadFile = File(description="待上传文件"),
    filename: Optional[str] = Form(default=None, description="覆盖原始文件名"),
    user: CurrentUser = Depends(get_current_user),
    service: FileService = Depends(get_file_service),
) -> SimpleUploadResponse:
    data = await file.read()
    info = await service.simple_upload(
        user,
        filename=filename or file.filename or "unnamed",
        content_type=file.content_type,
        data=data,
        max_size=SIMPLE_UPLOAD_LIMIT,
    )
    return SimpleUploadResponse(file=info)


@router.get("", response_model=PageResult[FileInfo], summary="查询我的文件列表")
async def list_files(
    keyword: Optional[str] = Query(default=None, max_length=100, description="按文件名模糊搜索"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(get_current_user),
    service: FileService = Depends(get_file_service),
) -> PageResult[FileInfo]:
    items, total = await service.list_files(user, keyword=keyword, limit=limit, offset=offset)
    return PageResult[FileInfo](
        items=items,
        pagination=Pagination(
            total=total, limit=limit, offset=offset, has_more=offset + len(items) < total
        ),
    )


@router.get("/{file_id}", response_model=FileInfo, summary="查询文件详情")
async def get_file(
    file_id: str = Path(description="文件 ID"),
    user: CurrentUser = Depends(get_current_user),
    service: FileService = Depends(get_file_service),
) -> FileInfo:
    return await service.get_file(user, file_id)


@router.get(
    "/{file_id}/download-url",
    response_model=DownloadUrlResponse,
    summary="获取临时下载地址",
    description="返回带签名的临时 URL，浏览器可直接下载，不消耗服务端带宽。",
)
async def get_download_url(
    file_id: str = Path(description="文件 ID"),
    expires: Optional[int] = Query(default=None, ge=60, le=86400, description="有效期（秒）"),
    inline: bool = Query(default=False, description="true 为浏览器内预览，false 为下载"),
    user: CurrentUser = Depends(get_current_user),
    service: FileService = Depends(get_file_service),
) -> DownloadUrlResponse:
    return await service.get_download_url(user, file_id, expires=expires, inline=inline)


@router.delete(
    "/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除文件",
    description="软删除数据库记录；当没有其它记录引用该对象时，同步删除 OSS 上的实际文件。",
)
async def delete_file(
    file_id: str = Path(description="文件 ID"),
    purge: bool = Query(default=True, description="是否在无引用时物理删除 OSS 对象"),
    user: CurrentUser = Depends(get_current_user),
    service: FileService = Depends(get_file_service),
) -> Response:
    await service.delete_file(user, file_id, purge=purge)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
