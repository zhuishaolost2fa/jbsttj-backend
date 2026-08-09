"""健康检查与配置自检。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.core.config import Settings, get_settings
from app.schemas.common import HealthResponse, ReadinessResponse
from app.services.oss import OSSService, get_oss_service
from app.services.supabase import SupabaseClient, get_supabase

router = APIRouter(tags=["系统"])

VERSION = "1.0.0"


@router.get("/health", response_model=HealthResponse, summary="存活探针")
async def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    return HealthResponse(
        status="ok", app=settings.app_name, env=settings.app_env, version=VERSION
    )


@router.get("/ready", response_model=ReadinessResponse, summary="就绪探针")
async def ready(
    response: Response,
    settings: Settings = Depends(get_settings),
    oss: OSSService = Depends(get_oss_service),
    db: SupabaseClient = Depends(get_supabase),
) -> ReadinessResponse:
    """真实探测 OSS 与 Supabase 连通性，任一不通返回 503。"""
    missing = settings.missing_required()
    checks = {"config": not missing, "oss": False, "supabase": False}

    if not missing:
        checks["oss"] = await oss.ping()
        checks["supabase"] = await db.ping()

    healthy = all(checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if healthy else "not_ready", checks=checks, missing_config=missing
    )
