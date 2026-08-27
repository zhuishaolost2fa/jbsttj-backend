"""剧本库接口。

读接口（列表 / 详情）公开，前端未登录也能浏览剧本；
写接口（新增 / 修改 / 下架）需要登录身份，走与文件模块一致的鉴权依赖。

字典编码从 `/api/v1/script-options` 拿，两个接口是配套使用的：
筛选器渲染用字典接口，筛选与提交时把 code 回传给本模块。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query, status

from app.core.security import CurrentUser, get_current_user, get_current_user_optional
from app.core.exceptions import AuthError
from app.schemas.common import MessageResponse
from app.schemas.script import (
    ScriptAutocompleteResult,
    ScriptCreate,
    ScriptCreateResult,
    ScriptItem,
    ScriptListResult,
    ScriptSearchByNameResult,
    ScriptUpdate,
)
from app.schemas.dm_guide import ImportStatus
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
    has_guide: Optional[bool] = Query(
        default=None, description="只看已关联 DM 主持人手册（已完成解析）的剧本"
    ),
    mine: bool = Query(
        default=False,
        description="只看我上传/创建的剧本（需登录，自动按当前用户过滤并包含草稿）",
    ),
    sort: str = Query(
        default="hot",
        pattern="^(hot|rating|newest|year|title)$",
        description="排序：hot=热度 / rating=评分 / newest=最新录入 / year=发行年份 / title=名称",
    ),
    limit: int = Query(default=20, ge=1, le=100, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    user: Optional[CurrentUser] = Depends(get_current_user_optional),
    service: ScriptService = Depends(get_script_service),
) -> ScriptListResult:
    # 「我的剧本」必须登录；草稿 / 下架记录对 RLS 不可见，所以这里强制带 created_by
    # 并放开 status 限制（不过滤 published），让本人能看到自己全部的导入记录。
    created_by = None
    if mine:
        if user is None:
            raise AuthError("查看我的剧本需要先登录", code="login_required")
        created_by = user.id

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
        has_guide=has_guide,
        sort=sort,
        limit=limit,
        offset=offset,
        created_by=created_by,
        # mine 时不过滤状态，展示本人全部剧本（含草稿）；非 mine 默认只看已上架
        status=None if mine else "published",
    )


@router.post(
    "",
    response_model=ScriptCreateResult,
    status_code=status.HTTP_201_CREATED,
    summary="新增 / 导入剧本",
    description=(
        "只有 `title` 必填。这是「新增剧本」与「导入 DM 指南」共用的入口。\n\n"
        "**导入去重关联**：若剧本库已存在同名（标题精确 / 别名精确）剧本，不会新建重复行，"
        "而是把本次导入的信息**关联**到已有剧本上（挂上 `extra.dmGuide`、补全缺失字段、"
        "置为已上架），并在响应里返回 `was_created=false`，前端据此提示「已关联到已有剧本」；"
        "若库里没有，则正常新建（`was_created=true`）。\n\n"
        "**code 冲突即补充**：`code` 是业务唯一键。若提交命中唯一约束 "
        "`uq_scripts_code`（包括并发写入撞车），本接口不会报错，而是把本次提交**补充更新**到"
        "已有那一行（身份字段 id/code/created_by/created_at 不变，标量字段以本次提交为准，"
        "数组字段取并集，`extra` 按 key 深合并），并同样返回 `was_created=false`。"
        "这让该接口对同一个 code 是幂等的——重复提交只更新、不报错。\n\n"
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
) -> ScriptCreateResult:
    script, was_created = await service.create_script(payload, user_id=user.id)
    # 放进后台任务而不是就地 await：派发要先查一次库、再往 MQ 投递，
    # 挂在请求链路上会让保存接口凭空多出几百毫秒。
    # maybe_trigger 内部吞掉全部异常 —— 手册解析失败绝不能让剧本保存看起来失败。
    background.add_task(dm.maybe_trigger, script, user_id=user.id)
    return ScriptCreateResult(**script.model_dump(), was_created=was_created)


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
    "/import-status",
    response_model=Dict[str, ImportStatus],
    response_model_by_alias=True,
    summary="批量查询剧本导入进度",
    description=(
        "「我的剧本」列表页专用：一次把多个剧本的导入进度（上传 → 解析 → 可问答）"
        "打包返回，替代列表里每个剧本各发一次 `GET /scripts/{id}/import-status` 轮询，"
        "把原本 N 次请求压成 1 次。\n\n"
        "`ids` 为逗号分隔的剧本 ID 或 code（最多 100 个），返回结构以 `scriptId` 为键。"
        "单个剧本查不到或计算失败时静默跳过，不会拖垮整批。"
    ),
)
async def batch_import_status(
    ids: str = Query(
        ..., min_length=1, max_length=4096, description="逗号分隔的剧本 ID 或 code 列表，最多 100 个"
    ),
    scripts: ScriptService = Depends(get_script_service),
    service: DMGuideService = Depends(get_dm_guide_service),
) -> Dict[str, ImportStatus]:
    # 去空白、去重、截断到 100，避免超长参数把后端问垮
    id_list = [x.strip() for x in ids.split(",") if x.strip()][:100]
    out: Dict[str, ImportStatus] = {}
    for id_or_code in id_list:
        try:
            script = await scripts.get_script(id_or_code)
            out[str(script.id)] = await service.get_import_status(script)
        except Exception:  # noqa: BLE001
            # 单个剧本异常（已删除 / 内部错误）不应让整批失败
            continue
    return out


@router.get(
    "/autocomplete",
    response_model=ScriptAutocompleteResult,
    summary="剧本名自动补全（联想搜索）",
    description=(
        "边输入边查的轻量搜索，供导入表单的下拉框「先选已有剧本再挂手册」使用，"
        "也供任意需要按名称快速检索剧本的场景。\n\n"
        "只按**已上架**剧本的标题（模糊）与别名（精确）召回，返回精简字段"
        "（id / code / title / author / cover_url / has_guide），不拉字典标签、不拼展示文案，"
        "保证实时响应。`has_guide=true` 表示该剧本已导入过 DM 手册，前端可据此提示避免重复导入。"
    ),
)
async def autocomplete_scripts(
    q: str = Query(
        ..., min_length=1, max_length=50, description="输入中的剧本名片段"
    ),
    limit: int = Query(default=8, ge=1, le=20, description="最多返回条数"),
    service: ScriptService = Depends(get_script_service),
) -> ScriptAutocompleteResult:
    items = await service.autocomplete(q, limit=limit)
    return ScriptAutocompleteResult(query=q, count=len(items), items=items)


@router.get(
    "/{id_or_code}",
    response_model=ScriptItem,
    summary="剧本详情",
    description=(
        "路径参数传剧本 UUID 或业务编码 code 均可。\n\n"
        "每次访问会把该剧本的浏览量 `view_count` +1（后台异步执行，不阻塞响应）。"
    ),
)
async def get_script(
    id_or_code: str = Path(description="剧本 UUID 或业务编码，如 `nian-lun`"),
    background: BackgroundTasks = BackgroundTasks(),
    service: ScriptService = Depends(get_script_service),
) -> ScriptItem:
    item = await service.get_script(id_or_code)
    # 响应返回后再异步计数，避免 +1 的 RPC 拖慢详情页首屏
    background.add_task(service.record_view, id_or_code)
    return item


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
    summary="删除剧本（含清理导入副作用）",
    description=(
        "删除后剧本从「我的剧本」列表移除（软删除，记录保留可人工恢复）。\n\n"
        "**同时清理导入剧本产生的副作用**：\n"
        "- 物理删除 DM 手册解析产物（解析任务 / 文档 / 分块 / 问答 / 用户提问 / "
        "故事还原 / 划线评论）；\n"
        "- 软删除上传的手册文件记录，并物理删除 OSS 上的手册对象"
        "（仅当没有其它文件记录或剧本仍在引用时）。"
    ),
)
async def delete_script(
    script_id: str = Path(description="剧本 UUID"),
    user: CurrentUser = Depends(get_current_user),
    service: ScriptService = Depends(get_script_service),
) -> MessageResponse:
    await service.delete_script(script_id, user_id=user.id)
    return MessageResponse(message="剧本已删除，导入数据已清理")
