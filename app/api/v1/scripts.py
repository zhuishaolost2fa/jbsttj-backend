"""剧本库接口。

读接口（列表 / 详情）公开，前端未登录也能浏览剧本；
写接口（新增 / 修改 / 下架）需要登录身份，走与文件模块一致的鉴权依赖。

字典编码从 `/api/v1/script-options` 拿，两个接口是配套使用的：
筛选器渲染用字典接口，筛选与提交时把 code 回传给本模块。
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query, status

from app.core.security import CurrentUser, get_current_user
from app.schemas.common import MessageResponse
from app.schemas.script import (
    ScriptCreate,
    ScriptItem,
    ScriptListResult,
    ScriptSearchByNameResult,
    ScriptUpdate,
)
from app.services.dm_service import DMGuideService, get_dm_guide_service
from app.services.script_service import ScriptService, get_script_service

router = APIRouter(prefix="/scripts", tags=["剧本库"])


@router.get(
    "",
    response_model=ScriptListResult,
    summary="剧本列表",
    description=(
        "支持关键词搜索与多维度筛选，全部筛选值均为 `/api/v1/script-options` 下发的字典编码。\n\n"
        "- 玩法、题材可传多个，命中任意一个即算匹配；\n"
        "- `players` / `duration` 传具体数值，后端按剧本的人数、时长区间做包含匹配；\n"
        "  例：`players=6` 会匹配到「6-7人」的剧本，`duration=300` 会匹配到「4-6小时」的剧本。\n\n"
        "例：`/api/v1/scripts?playstyle=emotional&theme=ancient&players=6&sort=rating`"
    ),
)
async def list_scripts(
    keyword: Optional[str] = Query(
        default=None, max_length=50, description="按剧本名、简介、作者、发行方、别名模糊搜索"
    ),
    playstyle: Optional[List[str]] = Query(default=None, description="玩法编码，可重复传参多选"),
    theme: Optional[List[str]] = Query(default=None, description="题材编码，可重复传参多选"),
    release: Optional[List[str]] = Query(default=None, description="发行方式编码，可多选"),
    difficulty: Optional[List[str]] = Query(default=None, description="难度编码，可多选"),
    players: Optional[int] = Query(default=None, ge=1, le=50, description="按人数匹配，如 6"),
    duration: Optional[int] = Query(
        default=None, ge=0, le=2880, description="按时长匹配，单位分钟，如 300"
    ),
    min_rating: Optional[float] = Query(default=None, ge=0, le=10, description="最低评分"),
    recommended_only: bool = Query(default=False, description="只看推荐位剧本"),
    sort: str = Query(
        default="hot",
        pattern="^(hot|rating|newest|year|title)$",
        description="排序：hot=热度 / rating=评分 / newest=最新录入 / year=发行年份 / title=名称",
    ),
    limit: int = Query(default=20, ge=1, le=100, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    service: ScriptService = Depends(get_script_service),
) -> ScriptListResult:
    return await service.list_scripts(
        keyword=keyword,
        playstyles=playstyle,
        themes=theme,
        release_types=release,
        difficulties=difficulty,
        players=players,
        duration=duration,
        min_rating=min_rating,
        recommended_only=recommended_only,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=ScriptItem,
    status_code=status.HTTP_201_CREATED,
    summary="新增剧本",
    description=(
        "只有 `title` 必填。\n\n"
        "- `code` 不传时由后端按标题自动生成，重名会自动加序号；\n"
        "- `playstyles` / `themes` / `release_type` / `difficulty` 必须是字典里真实存在的编码，"
        "传错会返回 422 并在 `details.allowed` 里列出全部可选值；\n"
        "- 人数与时长必须成对提供上下限；\n"
        "- `extra.dmGuide.objectKey` 里挂了 PDF 或 Word(.docx) 时，会在响应返回后自动触发手册解析入库，"
        "进度用 `GET /scripts/{id}/dm-guide` 查看。"
    ),
)
async def create_script(
    payload: ScriptCreate,
    background: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user),
    service: ScriptService = Depends(get_script_service),
    dm: DMGuideService = Depends(get_dm_guide_service),
) -> ScriptItem:
    script = await service.create_script(payload, user_id=user.id)
    # 放进后台任务而不是就地 await：派发要先查一次库、再往 MQ 投递，
    # 挂在请求链路上会让保存接口凭空多出几百毫秒。
    # maybe_trigger 内部吞掉全部异常 —— 手册解析失败绝不能让剧本保存看起来失败。
    background.add_task(dm.maybe_trigger, script, user_id=user.id)
    return script


@router.get(
    "/byname",
    response_model=ScriptSearchByNameResult,
    response_model_by_alias=True,
    summary="按名称查找剧本",
    description=(
        "前端传入剧本名称，后端在剧本库中按名称（标题 / 别名）模糊匹配，返回匹配到的剧本。\n\n"
        "匹配质量排序：标题精确命中 > 别名精确命中 > 标题前缀命中 > 标题包含命中。\n\n"
        "找不到时返回 `found=false`、`items=[]`（HTTP 200），前端无需区分 200/404，直接看 `found` 即可。\n\n"
        "**响应字段使用小驼峰（camelCase）**，如 `releaseType`、`playerMin`、`createdAt`，"
        "省去前端再做一层下划线→驼峰的映射。"
    ),
)
async def search_script_by_name(
    name: str = Query(
        ..., min_length=1, max_length=200, description="剧本名称，支持模糊匹配"
    ),
    limit: int = Query(default=10, ge=1, le=50, description="最多返回条数"),
    service: ScriptService = Depends(get_script_service),
) -> ScriptSearchByNameResult:
    items, found = await service.search_by_name(name, limit=limit)
    return ScriptSearchByNameResult(found=found, query=name, count=len(items), items=items)


@router.get(
    "/{id_or_code}",
    response_model=ScriptItem,
    summary="剧本详情",
    description="路径参数传剧本 UUID 或业务编码 code 均可。",
)
async def get_script(
    id_or_code: str = Path(description="剧本 UUID 或业务编码，如 `nian-lun`"),
    service: ScriptService = Depends(get_script_service),
) -> ScriptItem:
    return await service.get_script(id_or_code)


@router.patch(
    "/{script_id}",
    response_model=ScriptItem,
    summary="修改剧本（局部更新）",
    description=(
        "只提交需要变更的字段，未出现在请求体中的字段保持原值；"
        "显式传 `null` 表示把该字段清空。\n\n"
        "只改区间的一侧（如只传 `player_min`）会被拒绝，避免写出半截区间导致范围查询漏数据。\n\n"
        "更新后若 `extra.dmGuide.objectKey` 指向了一份尚未建索引的 PDF 或 Word(.docx)，会自动触发手册解析。"
    ),
)
async def patch_script(
    payload: ScriptUpdate,
    background: BackgroundTasks,
    script_id: str = Path(description="剧本 UUID"),
    user: CurrentUser = Depends(get_current_user),
    service: ScriptService = Depends(get_script_service),
    dm: DMGuideService = Depends(get_dm_guide_service),
) -> ScriptItem:
    script = await service.update_script(script_id, payload)
    background.add_task(dm.maybe_trigger, script, user_id=user.id)
    return script


@router.put(
    "/{script_id}",
    response_model=ScriptItem,
    summary="修改剧本（等价于 PATCH）",
    description="与 PATCH 行为一致，提供给习惯用 PUT 做更新的调用方。",
)
async def put_script(
    payload: ScriptUpdate,
    background: BackgroundTasks,
    script_id: str = Path(description="剧本 UUID"),
    user: CurrentUser = Depends(get_current_user),
    service: ScriptService = Depends(get_script_service),
    dm: DMGuideService = Depends(get_dm_guide_service),
) -> ScriptItem:
    script = await service.update_script(script_id, payload)
    background.add_task(dm.maybe_trigger, script, user_id=user.id)
    return script


@router.delete(
    "/{script_id}",
    response_model=MessageResponse,
    summary="下架剧本",
    description="软删除：置 `deleted_at` 并把状态改为 offline，记录仍保留在库中，可人工恢复。",
)
async def delete_script(
    script_id: str = Path(description="剧本 UUID"),
    _: CurrentUser = Depends(get_current_user),
    service: ScriptService = Depends(get_script_service),
) -> MessageResponse:
    await service.delete_script(script_id)
    return MessageResponse(message="剧本已下架")
