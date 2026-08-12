"""鉴权辅助接口。

正式前端建议直接用 supabase-js 完成登录，拿到 access_token 后调用本服务；
这里的代理接口主要方便 Swagger 调试、脚本化测试和不便集成 SDK 的客户端。

本模块还负责「个人资料」的读写与账号安全（改密码 / 改邮箱）：
- GET  /auth/me            取当前身份 + 个人资料
- PATCH /auth/me           部分更新个人资料（乐观并发，冲突返回 409）
- POST /auth/change-password  校验当前密码后修改登录密码
- POST /auth/change-email     校验当前密码后发起邮箱变更验证
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Request, UploadFile

from app.core.exceptions import (
    AuthError,
    ConflictError,
    DatabaseError,
    ValidationError,
)
from app.core.security import CurrentUser, get_current_user
from app.schemas.auth import (
    ChangeEmailRequest,
    ChangePasswordRequest,
    LoginRequest,
    MessageResponse,
    ProfileResponse,
    ProfileUpdate,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
)
from app.services.supabase import SupabaseAuth, SupabaseClient, get_supabase, get_supabase_auth
from app.services.file_service import FileService, get_file_service

logger = logging.getLogger("app.auth")

router = APIRouter(prefix="/auth", tags=["鉴权"])

AVATAR_MAX_BYTES = 2 * 1024 * 1024
_AVATAR_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _detect_image_type(data: bytes) -> Optional[str]:
    """嗅探文件头，识别真实图片类型，防止伪造扩展名 / content-type。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


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


async def _load_profile(db: SupabaseClient, user_id: str) -> Optional[Dict[str, Any]]:
    """读取用户个人资料；数据库不可用时返回 None 而不报错。"""
    if not db.available:
        return None
    try:
        return await db.select_one("profiles", filters={"id": f"eq.{user_id}"})
    except DatabaseError as exc:  # noqa: BLE001
        logger.warning("读取 profiles 失败（id=%s）: %s", user_id, exc)
        return None


async def _resolve_email_verified(auth: SupabaseAuth, user: CurrentUser) -> bool:
    """实时邮箱验证状态：以 GoTrue 管理接口的 email_confirmed_at 为准，
    token claim 作为接口不可用时的兜底。"""
    return await auth.get_email_verified(user.id) or bool(user.claims.get("email_verified"))


def _profile_response(
    user: CurrentUser,
    profile: Optional[Dict[str, Any]],
    email_verified: bool,
) -> ProfileResponse:
    """把鉴权身份与 profiles 行拼成统一的 ProfileResponse。"""
    return ProfileResponse(
        id=user.id,
        email=user.email,
        role=user.role,
        is_service=user.is_service,
        email_verified=email_verified,
        nickname=profile.get("nickname") if profile else None,
        avatar_url=profile.get("avatar_url") if profile else None,
        avatar_color=int(profile.get("avatar_color") or 0) if profile else 0,
        bio=profile.get("bio") if profile else None,
        gender=profile.get("gender") if profile else None,
        birthday=str(profile.get("birthday")) if profile and profile.get("birthday") else None,
        region=profile.get("region") if profile else None,
        created_at=profile.get("created_at") if profile else None,
        updated_at=profile.get("updated_at") if profile else None,
    )


@router.get("/me", response_model=ProfileResponse, summary="获取当前登录身份与资料")
async def me(
    user: CurrentUser = Depends(get_current_user),
    db: SupabaseClient = Depends(get_supabase),
    auth: SupabaseAuth = Depends(get_supabase_auth),
) -> ProfileResponse:
    profile = await _load_profile(db, user.id)
    email_verified = await _resolve_email_verified(auth, user)
    return _profile_response(user, profile, email_verified)


@router.patch("/me", response_model=ProfileResponse, summary="编辑个人资料")
async def update_me(
    payload: ProfileUpdate,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: SupabaseClient = Depends(get_supabase),
    auth: SupabaseAuth = Depends(get_supabase_auth),
) -> ProfileResponse:
    """部分更新当前用户的个人资料。

    - 只传需要修改的字段即可；未传的字段保持原值。
    - 显式传 null 表示清空该字段（例如把 avatar_url 设为 null 回到默认头像）。
    - 乐观并发：请求头带 ``If-Match: <updated_at>`` 时，若服务端资料已被他人改动
      （updated_at 不一致），返回 409（stale_profile），客户端应刷新后重试。
    - 资料保存进 public.profiles 表，下次 GET /auth/me 即生效，无需重新登录。
    """
    if not db.available:
        raise DatabaseError("数据库未配置，无法保存个人资料", code="db_unavailable")

    # model_dump(exclude_unset=True) 仅保留调用方显式给出的字段
    data = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
    if not data:
        raise ValidationError("至少需要提供一个要修改的字段（nickname / avatar_url / bio / gender / birthday / region）")

    # 乐观并发校验：以 updated_at 作为版本令牌
    if_match = request.headers.get("if-match")
    if if_match:
        current = await _load_profile(db, user.id)
        server_ts = (current or {}).get("updated_at")
        # 客户端回传的值可能带 HTTP 规范的引号，去掉后再比对
        client_ts = if_match.strip().strip('"')
        if server_ts is None or str(server_ts) != client_ts:
            raise ConflictError(
                "资料已被其他人修改，请刷新页面后重新编辑",
                code="stale_profile",
            )

    data["id"] = user.id
    rows = await db.upsert("profiles", data, on_conflict="id")
    profile = rows[0] if rows else {**(current or {}), **data}

    email_verified = await _resolve_email_verified(auth, user)
    return _profile_response(user, profile, email_verified)


@router.post("/me/avatar", response_model=ProfileResponse, summary="上传并更新头像")
async def upload_avatar(
    request: Request,
    file: UploadFile = File(..., description="头像图片，支持 JPG / PNG / WEBP，大小 ≤ 2MB"),
    user: CurrentUser = Depends(get_current_user),
    db: SupabaseClient = Depends(get_supabase),
    auth: SupabaseAuth = Depends(get_supabase_auth),
    service: FileService = Depends(get_file_service),
):
    """上传头像图片（裁剪由前端完成），服务端中转存储。

    - 校验格式（JPG/PNG/WEBP）、大小（≤2MB），并嗅探文件头防伪装。
    - 复用文件服务的 ``simple_upload`` 走服务端中转写入 OSS（含秒传去重），
      对象落在 ``avatars/`` 前缀下，key 记录在 ``profiles.avatar_object_key``，
      便于公开接口稳定回源；该前缀也不出现在用户的文件列表里。
    - ``avatar_url`` 统一使用后端代理地址 ``/api/v1/files/avatar/{user_id}``：
      该接口用服务端凭证读取 OSS，不依赖 bucket 公开读权限。裸 bucket 公网域名
      / CDN 仅在 bucket 或对象为 public-read 时才可达，而本项目的 OSS bucket
      默认不开启公共读，且阿里云默认启用「阻止公共访问」，直接返回 OSS URL 会
      403 无法展示；代理地址稳定、长期有效、无需鉴权，专为头像设计。
    - 成功后清空 avatar_color（已有真实图片，渐变头像不再需要）。
    """
    if not db.available:
        raise DatabaseError("数据库未配置，无法保存头像", code="db_unavailable")

    data = await file.read()
    if not data:
        raise ValidationError("图片内容为空", code="empty_avatar")
    if len(data) > AVATAR_MAX_BYTES:
        raise ValidationError("头像图片不能超过 2MB", code="avatar_too_large")
    if (file.content_type or "").lower() not in _AVATAR_ALLOWED_TYPES:
        raise ValidationError("仅支持 JPG / PNG / WEBP 格式的头像", code="bad_avatar_type")
    detected = _detect_image_type(data)
    if detected is None:
        raise ValidationError("图片内容无法识别，请更换图片后重试", code="bad_avatar")
    if detected != (file.content_type or "").lower():
        # 类型不一致不致命，但以嗅探到的真实类型入库，避免被伪造扩展名误导
        logger.warning("头像声明类型 %s 与实际 %s 不符", file.content_type, detected)

    # 复用文件服务的 simple_upload：服务端中转写入 OSS（含秒传去重）
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(detected, "jpg")
    info = await service.simple_upload(
        user,
        filename=f"avatar.{ext}",
        content_type=detected,
        data=data,
        max_size=AVATAR_MAX_BYTES,
        prefix="avatars",
    )
    object_key = info.object_key

    # 头像回源统一走后端代理接口（用服务端凭证读 OSS，不依赖 bucket 公开读权限，
    # 规避阿里云「阻止公共访问」导致的 403；接口见 files.py 的 get_avatar）。
    avatar_url = f"{str(request.base_url).rstrip('/')}/api/v1/files/avatar/{user.id}"
    rows = await db.upsert(
        "profiles",
        {
            "id": user.id,
            "avatar_url": avatar_url,
            "avatar_object_key": object_key,
            "avatar_color": None,
        },
        on_conflict="id",
    )
    profile = (
        rows[0]
        if rows
        else {"avatar_url": avatar_url, "avatar_object_key": object_key, "avatar_color": None}
    )
    logger.info("用户上传头像成功（id=%s, key=%s, url=%s）", user.id, object_key, avatar_url)
    email_verified = await _resolve_email_verified(auth, user)
    return _profile_response(user, profile, email_verified)


def _check_password_strength(password: str) -> Optional[str]:
    """返回密码强度问题文案；通过则返回 None。"""
    if len(password) < 6:
        return "密码至少 6 位"
    if password.strip() != password:
        return "密码不能包含首尾空格"
    if password.isdigit():
        return "密码不能为纯数字，请加入字母或符号"
    if password.lower() in {"password", "123456", "qwerty"}:
        return "该密码过于常见，请更换"
    return None


@router.post("/change-password", response_model=MessageResponse, summary="修改登录密码")
async def change_password(
    payload: ChangePasswordRequest,
    user: CurrentUser = Depends(get_current_user),
    auth: SupabaseAuth = Depends(get_supabase_auth),
) -> MessageResponse:
    """校验当前密码后修改登录密码。

    当前密码通过 GoTrue 登录接口确认；新密码需满足基本强度要求，
    再经 service_role 管理接口落地。改密后旧 token 仍然有效，
    下次登录需使用新密码（Supabase 默认会在改密后使旧会话失效，
    具体以 GoTrue 配置为准）。
    """
    weak = _check_password_strength(payload.new_password)
    if weak:
        raise ValidationError(weak)

    if not await auth.verify_password(user.email or "", payload.current_password):
        raise AuthError("当前密码不正确", code="invalid_credentials", status_code=401)

    await auth.admin_update_user(user.id, {"password": payload.new_password})
    logger.info("用户改密成功（id=%s）", user.id)
    return MessageResponse(message="密码已更新，下次登录请使用新密码")


@router.post("/change-email", response_model=MessageResponse, summary="修改登录邮箱")
async def change_email(
    payload: ChangeEmailRequest,
    user: CurrentUser = Depends(get_current_user),
    auth: SupabaseAuth = Depends(get_supabase_auth),
) -> MessageResponse:
    """校验当前密码后发起邮箱变更。

    新邮箱会通过 GoTrue 发送验证邮件，确认后才会正式生效；
    在确认前，登录邮箱仍为原邮箱。
    """
    new_email = payload.new_email.strip().lower()
    if user.email and new_email == user.email.lower():
        raise ValidationError("新邮箱不能与当前邮箱相同")

    if not await auth.verify_password(user.email or "", payload.current_password):
        raise AuthError("当前密码不正确", code="invalid_credentials", status_code=401)

    await auth.admin_update_user(user.id, {"email": new_email})
    logger.info("用户发起改邮箱（id=%s -> %s）", user.id, new_email)
    return MessageResponse(message="验证邮件已发送至新邮箱，确认后才会生效")
