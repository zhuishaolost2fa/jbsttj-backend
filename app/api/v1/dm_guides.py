"""DM 主持人手册接口：解析入库、进度查询、向量检索。

挂在剧本资源下（`/scripts/{id_or_code}/dm-guide`），因为一份手册永远从属于
一个剧本，没有脱离剧本单独存在的 DM 手册。

**读写权限的划分与剧本模块一致**：检索和进度查询是读操作，前端主持人页面
直接用；触发解析会消耗真金白银的 LLM 额度，必须登录。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Path, Query, status

from app.core.security import CurrentUser, get_current_user
from app.schemas.dm_guide import (
    DMGuideStatus,
    IngestRequest,
    IngestResponse,
    JobProgress,
    SearchResult,
)
from app.services.dm_service import DMGuideService, get_dm_guide_service
from app.services.script_service import ScriptService, get_script_service

router = APIRouter(prefix="/scripts", tags=["DM 主持人手册"])


@router.post(
    "/{id_or_code}/dm-guide/ingest",
    response_model=IngestResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
    summary="触发 DM 手册解析入库",
    description=(
        "把剧本关联的 DM 手册 PDF 解析、切块、生成问答对并写入向量库。\n\n"
        "**这是个异步接口**：返回 202 和 `jobId`，实际解析在后台队列执行。"
        "400 页的手册通常要十几分钟，用进度接口轮询。\n\n"
        "- 剧本上必须已有 `extra.dmGuide.objectKey`，否则返回 422；\n"
        "- 同一剧本已有任务在跑时不会重复派发，直接返回那个任务（`reused=true`）；\n"
        "- `force=true` 会取消在跑的任务并强制重算，**会重新消耗全额 LLM 与向量化额度**，"
        "仅在换了模型或分块参数时使用。\n\n"
        "正常业务流程下不需要手动调用 —— 保存剧本时检测到 `extra.dmGuide` 变化会自动触发。"
    ),
)
async def ingest_dm_guide(
    payload: Optional[IngestRequest] = None,
    id_or_code: str = Path(description="剧本 UUID 或业务编码"),
    user: CurrentUser = Depends(get_current_user),
    scripts: ScriptService = Depends(get_script_service),
    service: DMGuideService = Depends(get_dm_guide_service),
) -> IngestResponse:
    script = await scripts.get_script(id_or_code)
    return await service.trigger_ingest(script, payload, user_id=user.id)


@router.get(
    "/{id_or_code}/dm-guide",
    response_model=DMGuideStatus,
    response_model_by_alias=True,
    summary="DM 手册索引状态",
    description=(
        "剧本详情页用：手册传了没、索引建好没、上次解析是成功还是失败。\n\n"
        "`indexed=true` 才代表可以检索。`job` 是最近一次解析任务的进度快照，"
        "失败时 `job.errorMessage` 里有原因。"
    ),
)
async def get_dm_guide_status(
    id_or_code: str = Path(description="剧本 UUID 或业务编码"),
    scripts: ScriptService = Depends(get_script_service),
    service: DMGuideService = Depends(get_dm_guide_service),
) -> DMGuideStatus:
    script = await scripts.get_script(id_or_code)
    return await service.get_status(script)


@router.get(
    "/{id_or_code}/dm-guide/jobs/{job_id}",
    response_model=JobProgress,
    response_model_by_alias=True,
    summary="解析任务进度",
    description=(
        "轮询这个接口跟踪解析进度。建议 3-5 秒一次。\n\n"
        "**不要用单一百分比展示进度**：四个阶段的耗时差着两个数量级"
        "（提取几十秒、问答生成十几分钟），线性插值出来的百分比会长时间卡在某个数字上，"
        "看起来像卡死了。按 `status` 分阶段展示，每阶段用对应的计数字段做子进度："
        "提取看 `processedPages/totalPages`，向量化看 `embeddedChunks/totalChunks`。\n\n"
        "`status` 进入 `completed` / `failed` / `cancelled` / `skipped` 后即可停止轮询。"
        "`skipped` 表示内容指纹命中了已完成的旧版本，直接复用了现有索引。"
    ),
)
async def get_dm_guide_job(
    id_or_code: str = Path(description="剧本 UUID 或业务编码"),
    job_id: str = Path(description="任务 ID"),
    service: DMGuideService = Depends(get_dm_guide_service),
) -> JobProgress:
    return await service.get_job(job_id)


@router.get(
    "/{id_or_code}/dm-guide/search",
    response_model=SearchResult,
    response_model_by_alias=True,
    summary="DM 手册语义检索",
    description=(
        "按语义检索手册内容，供主持人在带本过程中即时查规则。\n\n"
        "**三种模式**：\n"
        "- `qa` —— 只查预生成的问答对，命中即得直接答案，适合「玩家问 X 怎么办」这类明确问题；\n"
        "- `chunk` —— 只查原文分块，覆盖面全，返回结果带章节面包屑与页码，适合查证原文；\n"
        "- `hybrid`（默认）—— 两者都查。推荐用法：先展示 QA 的答案，再用原文块给出处。\n\n"
        "结果按余弦相似度倒序。`minSimilarity` 调低会召回更多但更杂的结果；"
        "中文语义检索里低于 0.25 的命中基本是噪声，不建议再往下调。"
    ),
)
async def search_dm_guide(
    id_or_code: str = Path(description="剧本 UUID 或业务编码"),
    q: str = Query(min_length=1, max_length=500, description="检索问题，用自然语言描述"),
    mode: str = Query(
        default="hybrid",
        pattern="^(chunk|qa|hybrid)$",
        description="检索模式：chunk=原文块 / qa=问答对 / hybrid=两者都查",
    ),
    top_k: int = Query(default=8, ge=1, le=50, alias="topK", description="每类结果的最大条数"),
    min_similarity: Optional[float] = Query(
        default=None,
        ge=0,
        le=1,
        alias="minSimilarity",
        description="相似度下限，不传用服务端默认值",
    ),
    category: Optional[str] = Query(
        default=None, description="只看某一类问答，仅对 qa / hybrid 模式生效"
    ),
    scripts: ScriptService = Depends(get_script_service),
    service: DMGuideService = Depends(get_dm_guide_service),
) -> SearchResult:
    script = await scripts.get_script(id_or_code)
    return await service.search(
        query=q,
        script_id=str(script.id),
        mode=mode,
        top_k=top_k,
        min_similarity=min_similarity,
        category=category,
    )
