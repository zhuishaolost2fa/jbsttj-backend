"""鉴权辅助接口。

正式前端建议直接用 supabase-js 完成登录，拿到 access_token 后调用本服务；
这里的代理接口主要方便 Swagger 调试、脚本化测试和不便集成 SDK 的客户端。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from app.core.security import CurrentUser, get_current_user
from app.schemas.auth import (
    LoginRequest,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
)
from app.services.supabase import SupabaseAuth, get_supabase_auth

router = APIRouter(prefix="/auth", tags=["鉴权"])


def _to_token(data: Dict[str, Any]) -> TokenResponse:
    return TokenResponse(
        access_token=data.get("access_token", ""),
        refresh_token=data.get("refresh_token"),
        expires_in=data.get("expires_in"),
        user=data.get("user"),
    )


@router.post("/register", response_model=TokenResponse, summary="注册账号")
async def register(
    payload: RegisterRequest,
    auth: SupabaseAuth = Depends(get_supabase_auth),
) -> TokenResponse:
    data = await auth.sign_up(payload.email, payload.password)
    # 开启邮箱验证时不会立刻返回 token，此处 access_token 可能为空
    return _to_token(data)


@router.post("/login", response_model=TokenResponse, summary="邮箱密码登录")
async def login(
    payload: LoginRequest,
    auth: SupabaseAuth = Depends(get_supabase_auth),
) -> TokenResponse:
    data = await auth.sign_in(payload.email, payload.password)
    return _to_token(data)


@router.post("/refresh", response_model=TokenResponse, summary="刷新 access token")
async def refresh(
    payload: RefreshRequest,
    auth: SupabaseAuth = Depends(get_supabase_auth),
) -> TokenResponse:
    data = await auth.refresh(payload.refresh_token)
    return _to_token(data)


@router.get("/me", response_model=MeResponse, summary="获取当前登录身份")
async def me(user: CurrentUser = Depends(get_current_user)) -> MeResponse:
    return MeResponse(id=user.id, email=user.email, role=user.role, is_service=user.is_service)
