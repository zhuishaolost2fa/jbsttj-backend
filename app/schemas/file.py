"""文件资源模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class FileInfo(BaseModel):
    id: str
    filename: str
    object_key: str
    bucket: str
    content_type: Optional[str] = None
    file_size: int
    file_hash: Optional[str] = None
    etag: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "FileInfo":
        return cls(
            id=str(row["id"]),
            filename=row.get("filename") or "",
            object_key=row.get("object_key") or "",
            bucket=row.get("bucket") or "",
            content_type=row.get("content_type"),
            file_size=int(row.get("file_size") or 0),
            file_hash=row.get("file_hash"),
            etag=row.get("etag"),
            metadata=row.get("metadata") or {},
            created_at=row.get("created_at"),
        )


class DownloadUrlResponse(BaseModel):
    file_id: str
    filename: str
    url: str
    expires_in: int
    inline: bool = False


class SimpleUploadResponse(BaseModel):
    file: FileInfo
    message: str = "上传成功"
