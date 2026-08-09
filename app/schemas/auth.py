"""鉴权辅助接口模型（调试与轻量前端用）。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    expires_in: Optional[int] = None
    user: Optional[Dict[str, Any]] = None


class MeResponse(BaseModel):
    id: str
    email: Optional[str] = None
    role: str
    is_service: bool = False
