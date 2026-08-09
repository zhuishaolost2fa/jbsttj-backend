"""分片上传相关的请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.file import FileInfo


class InitUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255, description="原始文件名")
    file_size: int = Field(gt=0, description="文件总字节数")
    content_type: Optional[str] = Field(default=None, description="MIME 类型，可不传由服务端推断")
    upload_type: Literal["temporary", "permanent"] = Field(
        default="permanent",
        description=(
            "temporary=临时文件，写入 temp/ 前缀，受 OSS 生命周期规则过期清理；"
            "permanent=永久文件，写入 uploads/ 前缀，不被自动清理。"
        ),
    )
    file_hash: Optional[str] = Field(
        default=None,
        max_length=128,
        description="文件内容指纹（推荐 SHA-256，大文件可用抽样哈希），用于秒传与断点续传匹配",
    )
    chunk_size: Optional[int] = Field(
        default=None, ge=100 * 1024, description="期望分片大小，服务端可能会调整"
    )
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="附加业务元数据")

    @field_validator("file_hash")
    @classmethod
    def _normalize_hash(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().lower() if v else None


class UploadedPart(BaseModel):
    part_number: int
    etag: str
    size: int = 0


class InitUploadResponse(BaseModel):
    task_id: str = Field(description="上传任务 ID，后续所有分片操作都基于它")
    object_key: str
    upload_id: Optional[str] = Field(default=None, description="OSS 分片上传 ID，秒传时为空")
    chunk_size: int = Field(description="实际分片大小，前端必须按此切片")
    total_parts: int
    file_size: int
    status: str
    instant: bool = Field(default=False, description="true 表示命中秒传，无需上传")
    resumed: bool = Field(default=False, description="true 表示命中断点续传")
    upload_type: str = Field(
        default="permanent", description="temporary 或 permanent，用于区分是否受 OSS 生命周期清理"
    )
    uploaded_parts: List[UploadedPart] = Field(
        default_factory=list, description="已经上传成功的分片，前端应跳过这些分片"
    )
    file: Optional[FileInfo] = Field(default=None, description="秒传命中时直接返回文件信息")
    part_content_type: str = Field(
        default="application/octet-stream",
        description="上传分片时必须设置的 Content-Type，与签名保持一致",
    )


class PresignPartsRequest(BaseModel):
    part_numbers: List[int] = Field(
        min_length=1, description="需要签名的分片序号列表，从 1 开始"
    )
    expires: Optional[int] = Field(default=None, ge=60, le=86400, description="URL 有效期（秒）")

    @field_validator("part_numbers")
    @classmethod
    def _validate_parts(cls, v: List[int]) -> List[int]:
        if any(n < 1 or n > 10000 for n in v):
            raise ValueError("分片序号必须在 1..10000 之间")
        return sorted(set(v))


class PresignedPart(BaseModel):
    part_number: int
    url: str


class PresignPartsResponse(BaseModel):
    task_id: str
    object_key: str
    upload_id: str
    expires_in: int
    part_content_type: str = "application/octet-stream"
    parts: List[PresignedPart]


class PartCallbackRequest(BaseModel):
    etag: str = Field(min_length=1, description="OSS 返回的分片 ETag")
    size: int = Field(ge=0, default=0, description="该分片实际字节数")

    @field_validator("etag")
    @classmethod
    def _strip_quotes(cls, v: str) -> str:
        return v.strip().strip('"')


class BatchPartCallbackRequest(BaseModel):
    parts: List[UploadedPart] = Field(min_length=1, description="批量上报已完成的分片")


class CompleteUploadRequest(BaseModel):
    parts: Optional[List[UploadedPart]] = Field(
        default=None,
        description="可选。不传时服务端以 OSS 实际列举到的分片为准（更可靠）",
    )
    verify_size: bool = Field(default=True, description="是否校验合并后大小与声明一致")


class TaskStatusResponse(BaseModel):
    task_id: str
    object_key: str
    upload_id: Optional[str] = None
    filename: str
    file_size: int
    chunk_size: int
    total_parts: int
    status: str
    uploaded_parts: List[UploadedPart] = Field(default_factory=list)
    uploaded_bytes: int = 0
    progress: float = Field(default=0.0, description="0~100 的进度百分比")
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TaskBrief(BaseModel):
    task_id: str
    filename: str
    file_size: int
    total_parts: int
    status: str
    created_at: Optional[datetime] = None


class CompleteUploadResponse(BaseModel):
    task_id: str
    file: FileInfo
    message: str = "上传完成"
