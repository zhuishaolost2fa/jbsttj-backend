"""DM 主持人手册接口：解析入库、进度查询、向量检索。

挂在剧本资源下（`/scripts/{id_or_code}/dm-guide`），因为一份手册永远从属于
一个剧本，没有脱离剧本单独存在的 DM 手册。

**读写权限的划分与剧本模块一致**：检索和进度查询是读操作，前端主持人页面
直接用；触发解析会消耗真金白银的 LLM 额度，必须登录。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Path, Query, status

from app.core.exceptions import ValidationError
from app.core.security import CurrentUser, get_current_user, get_current_user_optional
from app.schemas.dm_guide import (
    AskRequest,
    AskResponse,
    DMGuideStatus,
    ImportStatus,
    IngestRequest,
    IngestResponse,
    JobProgress,
    SearchResult,
)
from app.services.dm_service import DMGuideService, get_dm_guide_service, script_dm_code
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
    "/{id_or_code}/import-status",
    response_model=ImportStatus,
    summary="剧本导入整体进度",
    description=(
        "把「上传手册 → 解析入库 → 可问答」三个语义阶段归一成一个轮询接口，\n"
        "用户上传剧本后**只需轮询这一个**就能感知整体进度，不必分别盯上传接口和解析接口。\n\n"
        "三阶段状态：`pending` 未开始 / `active` 进行中 / `done` 完成 / "
        "`failed` 失败 / `cancelled` 已取消 / `skipped` 复用旧索引。\n"
        "`overall_status` 取值：`uploading` 上传中 / `parsing` 解析中 / "
        "`ready` 可问答 / `failed` 失败 / `no_guide` 未传手册 / `pending` 待开始。\n\n"
        "实时字节级上传进度（传输中的几秒）由 `GET /api/v1/uploads/{task_id}` 负责；\n"
        "传 `uploadTaskId` 可把这个实时进度并入 `upload` 字段，实现「一个接口看全部」。"
    ),
)
async def get_import_status(
    id_or_code: str = Path(description="剧本 UUID 或业务编码"),
    upload_task_id: Optional[str] = Query(
        default=None, description="可选：上传任务 ID，传入则把实时字节进度并入 upload 字段"
    ),
    scripts: ScriptService = Depends(get_script_service),
    service: DMGuideService = Depends(get_dm_guide_service),
) -> ImportStatus:
    script = await scripts.get_script(id_or_code)
    return await service.get_import_status(script, upload_task_id=upload_task_id)


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
        script_code=script_dm_code(script),
        mode=mode,
        top_k=top_k,
        min_similarity=min_similarity,
        category=category,
    )


@router.post(
    "/{id_or_code}/dm-guide/ask",
    response_model=AskResponse,
    summary="剧本详情问答（向量直出，速度最优）",
    description=(
        "检索手册相关内容，返回答案。\n\n"
        "**速度优先（默认）**：`useLlm` 不传或为 `false` 时**不调用大模型**，直接把向量检索到的\n"
        "最近命中（qa 答案或原文块）透出为 `answer`，端到端只花一次 embedding + 向量查询，\n"
        "几十毫秒级、零 LLM 额度，适合带本时高频即时查规则。\n\n"
        "需要 LLM 把命中内容**合成**成一条带引用出处的答案时，把 `useLlm` 设为 `true`\n"
        "（会消耗 LLM 额度，速度更慢）。\n\n"
        "与 `search` 的区别：search 只返回原始命中（前端自己挑），ask 额外把答案凝练成\n"
        "一条可直接照着念的文本，并在 `sources` 里给出每条结论对应的章节与页码。\n\n"
        "前置条件同检索：剧本手册必须已完成索引（`indexed=true`），否则返回 409。需登录调用。"
    ),
)
async def ask_dm_guide(
    payload: AskRequest,
    id_or_code: str = Path(description="剧本 UUID 或业务编码"),
    user: CurrentUser = Depends(get_current_user),
    scripts: ScriptService = Depends(get_script_service),
    service: DMGuideService = Depends(get_dm_guide_service),
) -> AskResponse:
    script = await scripts.get_script(id_or_code)
    return await service.ask(
        question=payload.question,
        script=script,
        mode=payload.mode,
        top_k=payload.top_k,
        min_similarity=payload.min_similarity,
        category=payload.category,
        use_llm=payload.use_llm,
    )


# ------------------------------------------------------------
# 扁平问答接口
# ------------------------------------------------------------
# 与前缀 /scripts 的「路径式」ask 功能完全一致，只是把剧本标识从 URL 路径
# 挪到请求体：前端只需知道剧本的 `code`（或 UUID）加上 `询问` 即可发起检索，
# 不必先拼出 `/scripts/{id_or_code}` 路径。这样前端在任何拿到剧本 code 的
# 场景（列表项、详情页、带本页）都能直接调，无需再额外维护 scriptId 映射。
ask_router = APIRouter(prefix="/dm-guide", tags=["DM 主持人手册"])


@ask_router.post(
    "/ask",
    response_model=AskResponse,
    summary="剧本问答（按 code 直接检索，向量直出）",
    description=(
        "与 `POST /scripts/{id_or_code}/dm-guide/ask` 功能完全一致，只是把剧本标识从 URL 路径\n"
        "挪到了请求体：前端只需知道剧本的 `code`（或 UUID）加上 `询问`，即可发起检索，\n"
        "不必先拼出 `/scripts/{id_or_code}` 路径。\n\n"
        "请求体字段：`code`（**必填**，界定检索范围）、`询问`/`question`（**必填**，自然语言问题）、\n"
        "`mode`、`topK`、`minSimilarity`、`category`（均可选，含义同检索接口）、\n"
        "`useLlm`（可选，默认 `false`）。\n\n"
        "后端先用 `code` 解析出剧本，再用 `询问` 向量化后在**该剧本手册内**做向量检索。\n"
        "**默认 `useLlm=false`：不调大模型，直接把向量最近命中的答案文本返回，速度最优、零 LLM 额度**；\n"
        "需要 LLM 合成带引用出处的答案时再设 `useLlm=true`。检索范围严格限定在 `code` 对应的手册，\n"
        "不会跨剧本串味。前置条件同检索：手册须已完成索引（`indexed=true`），否则返回 409。"
    ),
)
async def ask_dm_guide_by_code(
    payload: AskRequest,
    user: CurrentUser = Depends(get_current_user),
    scripts: ScriptService = Depends(get_script_service),
    service: DMGuideService = Depends(get_dm_guide_service),
) -> AskResponse:
    code = (payload.code or "").strip()
    if not code:
        raise ValidationError("code 不能为空，请传入剧本业务编码或 UUID", code="code_required")
    # question 已由 AskRequest 的校验器保证非空
    script = await scripts.get_script(code)
    return await service.ask(
        question=payload.question,
        script=script,
        mode=payload.mode,
        top_k=payload.top_k,
        min_similarity=payload.min_similarity,
        category=payload.category,
        use_llm=payload.use_llm,
    )
