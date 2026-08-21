"""剧本「求解析」接口。

语义：
- 用户对某个剧本发起「希望解析」诉求（剧本可在库、也可仅凭标题）；
- **同一用户对同一剧本只能求一次** —— 重复发起返回既有记录（幂等），
  已取消的可「复活」重新求；
- 剧本被解析完成后，对应诉求自动流转为 `completed`（读取时惰性同步），
  并从排行榜剔除；
- 排行榜：全站 pending 诉求按剧本聚合的「希望解析」诉求榜单。

注册顺序注意：本 router 挂 `/scripts/requests` 前缀，必须在 `/scripts`
（含 `/{id_or_code}` 单段路由）之前 include，避免「requests」被误匹配成
剧本 ID。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Path, Query, status

from app.core.security import CurrentUser, get_current_user, get_current_user_optional
from app.schemas.script_request import (
    ScriptRequestCreate,
    ScriptRequestItem,
    ScriptRequestLeaderboardResult,
    ScriptRequestListResult,
)
from app.services.script_request_service import (
    ScriptRequestService,
    get_script_request_service,
)

router = APIRouter(prefix="/scripts/requests", tags=["求解析"])


@router.post(
    "",
    response_model=ScriptRequestItem,
    status_code=status.HTTP_201_CREATED,
    summary="发起求解析",
    description=(
        "对某个剧本发起「希望解析」诉求。**同一用户对同一剧本只能求一次**：\n\n"
        "- 目标剧本三选一：`scriptId`（剧本 UUID）、`scriptCode`（业务编码）、"
        "或仅 `scriptTitle`（先按名称匹配剧本库，未命中则作为库外诉求保留标题）；\n"
        "- 重复发起（仍 pending）返回既有记录，响应里 `alreadyExists=true`；\n"
        "- 已取消的诉求再次发起会**复活**回待解析，不新建行；\n"
        "- 剧本已解析完成时返回 409 `script_already_parsed`，无需再求。"
    ),
)
async def create_request(
    payload: ScriptRequestCreate,
    user: CurrentUser = Depends(get_current_user),
    service: ScriptRequestService = Depends(get_script_request_service),
) -> ScriptRequestItem:
    return await service.create(user.id, payload)


@router.get(
    "/me",
    response_model=ScriptRequestListResult,
    summary="我的求解析列表",
    description=(
        "当前用户的求解析诉求（分页）。可传 `status` 过滤\n"
        "（pending 待解析 / completed 已完成 / cancelled 已取消）。\n\n"
        "返回前会先把「剧本已被解析」的诉求自动流转为 completed，"
        "因此 `completed` 项即表示该剧本已经解析完成。"
    ),
)
async def list_my_requests(
    status_filter: Optional[str] = Query(
        default=None, alias="status", description="按状态过滤：pending / completed / cancelled"
    ),
    limit: int = Query(default=20, ge=1, le=100, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    user: CurrentUser = Depends(get_current_user),
    service: ScriptRequestService = Depends(get_script_request_service),
) -> ScriptRequestListResult:
    return await service.list_mine(
        user.id, status=status_filter, limit=limit, offset=offset
    )


@router.get(
    "/leaderboard",
    response_model=ScriptRequestLeaderboardResult,
    summary="求解析排行榜",
    description=(
        "全站「希望解析剧本」诉求榜单：按剧本聚合 **pending** 诉求人数降序排列，"
        "已取消与已解析完成的诉求不计入榜单。无需登录。"
    ),
)
async def leaderboard(
    limit: int = Query(default=20, ge=1, le=100, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    _: Optional[CurrentUser] = Depends(get_current_user_optional),
    service: ScriptRequestService = Depends(get_script_request_service),
) -> ScriptRequestLeaderboardResult:
    return await service.leaderboard(limit=limit, offset=offset)


@router.delete(
    "/{request_id}",
    response_model=ScriptRequestItem,
    summary="取消求解析",
    description=(
        "取消自己的求解析诉求（软取消，不删行，可再次发起复活）。\n"
        "已取消的重复取消幂等返回现状；已完成的诉求不能取消（409）。"
    ),
)
async def cancel_request(
    request_id: str = Path(description="求解析记录 ID"),
    user: CurrentUser = Depends(get_current_user),
    service: ScriptRequestService = Depends(get_script_request_service),
) -> ScriptRequestItem:
    return await service.cancel(user.id, request_id)
