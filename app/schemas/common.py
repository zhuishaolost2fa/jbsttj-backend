"""通用响应结构。"""

from __future__ import annotations

from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Pagination(BaseModel):
    total: int = Field(description="总记录数")
    limit: int = Field(description="每页条数")
    offset: int = Field(description="偏移量")
    has_more: bool = Field(description="是否还有下一页")


class PageResult(BaseModel, Generic[T]):
    items: List[T]
    pagination: Pagination


class MessageResponse(BaseModel):
    success: bool = True
    message: str = "ok"


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Optional[object] = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: str
    app: str
    env: str
    version: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict
    missing_config: List[str] = []
