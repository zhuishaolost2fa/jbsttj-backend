"""STS 临时凭证下发接口。

当 OSS 采用 STS 接入（`OSS_USE_STS=true`）时，本接口把**短期、可过期**的
OSS 临时凭证下发给已登录的前端，前端即可用 OSS 浏览器 SDK 直传，
无需经过本服务的预签名环节。长期 AccessKey 永不离开服务端。

安全说明
--------
- 接口强制鉴权（必须携带合法用户 token / 服务间调用凭证）。
- 临时凭证有效期由 `OSS_STS_DURATION_SECONDS` 控制（默认 1 小时），到期自动失效。
- 实际权限由 RAM 角色 `OSS_STS_ROLE_ARN` 绑定的策略决定，建议按 bucket/前缀最小化授权。
- 若不想把凭证下发到浏览器，可不使用本接口，改走「后端 STS + 预签名 URL」模式
  （前端只拿预签名 PUT 地址，见 uploads 路由），两种模式共用同一套 STS 接入。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigError
from app.core.security import CurrentUser, get_current_user
from app.services.sts import STSClient, get_sts_client

router = APIRouter(prefix="/sts", tags=["STS"])


class STSTokenResponse(BaseModel):
    access_key_id: str
    access_key_secret: str
    security_token: str
    expiration: datetime
    bucket: str
    region: str
    endpoint: str
    prefix: str


@router.post("/token", response_model=STSTokenResponse, summary="获取 OSS 直传临时凭证")
async def issue_sts_token(
    user: CurrentUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> STSTokenResponse:
    """为已登录用户签发用于浏览器直传 OSS 的 STS 临时凭证。

    返回的 `prefix` 建议作为上传对象的 key 前缀（如 `{upload_prefix}/{user_id}/...`），
    便于后端按用户隔离、并配合角色策略做最小权限授权。
    """
    if not settings.oss_use_sts:
        raise ConfigError("当前未启用 STS（OSS_USE_STS=false），无法下发临时凭证", code="sts_disabled")

    sts: Optional[STSClient] = get_sts_client(settings)
    if sts is None:
        raise ConfigError("STS 客户端初始化失败", code="sts_unavailable")

    creds = await run_in_threadpool(sts.get_credentials_sync)
    prefix = f"{settings.upload_prefix}/{user.id}"

    return STSTokenResponse(
        access_key_id=creds.access_key_id,
        access_key_secret=creds.access_key_secret,
        security_token=creds.security_token,
        expiration=creds.expiration,
        bucket=settings.oss_bucket,
        region=settings.oss_region,
        endpoint=settings.oss_public_endpoint,
        prefix=prefix,
    )
