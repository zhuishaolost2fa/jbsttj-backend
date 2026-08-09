"""剧本杀筛选维度字典接口。

这些是**公开只读的参考数据**（玩法/题材/发行方式/难度/人数/时长），
不含任何用户隐私，因此不挂鉴权依赖 —— 前端在未登录状态下也要能渲染筛选器。
仅「刷新缓存」这一个写操作需要登录身份。
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Path, Query

from app.core.security import CurrentUser, get_current_user
from app.data.script_options_seed import CATEGORY_CODES
from app.schemas.common import MessageResponse
from app.schemas.script_option import (
    ScriptOptionCategory,
    ScriptOptionListResult,
    ScriptOptionTree,
)
from app.services.script_option_service import (
    ScriptOptionService,
    get_script_option_service,
)

router = APIRouter(prefix="/script-options", tags=["剧本杀选项"])

_CATEGORY_DESC = "筛选维度编码，可选值：" + " / ".join(CATEGORY_CODES)


@router.get(
    "",
    response_model=ScriptOptionTree,
    summary="获取全部筛选维度及选项",
    description=(
        "一次性返回全部维度与其下选项，**前端首屏渲染筛选器只需调这一个接口**。\n\n"
        "「人数」「时长」维度的选项额外带 `min_value` / `max_value` / `unit`，"
        "可直接用于范围过滤，前端无需硬编码数字。"
    ),
)
async def get_option_tree(
    only_hot: bool = Query(default=False, description="只返回热门选项，用于收起态的精简筛选器"),
    service: ScriptOptionService = Depends(get_script_option_service),
) -> ScriptOptionTree:
    return await service.get_tree(only_hot=only_hot)


@router.get(
    "/categories",
    response_model=List[ScriptOptionCategory],
    summary="获取筛选维度列表",
    description="仅返回维度本身（不含选项），适合先渲染筛选器 Tab、再按需拉取选项。",
)
async def list_categories(
    service: ScriptOptionService = Depends(get_script_option_service),
) -> List[ScriptOptionCategory]:
    return await service.list_categories()


@router.post(
    "/refresh",
    response_model=MessageResponse,
    summary="刷新字典缓存",
    description="字典数据带 10 分钟进程内缓存；在后台改动选项后调用本接口可立即生效。",
)
async def refresh_cache(
    _: CurrentUser = Depends(get_current_user),
    service: ScriptOptionService = Depends(get_script_option_service),
) -> MessageResponse:
    service.invalidate_cache()
    return MessageResponse(message="字典缓存已刷新")


# 注意：本路由必须放在 /categories、/refresh 之后声明，
# 否则 "categories" 会被当作 category_code 匹配掉。
@router.get(
    "/{category_code}",
    response_model=ScriptOptionListResult,
    summary="获取单个维度的选项列表",
    description=(
        "按维度取选项，支持关键词过滤与只看热门。\n\n"
        "例：`/api/v1/script-options/playstyle?keyword=情感`"
    ),
)
async def list_options(
    category_code: str = Path(description=_CATEGORY_DESC),
    only_hot: bool = Query(default=False, description="只返回热门选项"),
    keyword: Optional[str] = Query(
        default=None, max_length=50, description="按标签、说明或别名模糊匹配"
    ),
    service: ScriptOptionService = Depends(get_script_option_service),
) -> ScriptOptionListResult:
    return await service.list_options(category_code, only_hot=only_hot, keyword=keyword)
