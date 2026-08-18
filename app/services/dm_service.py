"""DM 指南 RAG 的业务编排层。

夹在 HTTP 接口与「Celery 流水线 / 向量库」之间，负责三件事：

  1. **触发前的守门** —— 配置齐不齐、文件在不在、体积超没超、是不是已经在跑了。
     这些校验必须在 HTTP 侧同步完成：一旦任务派进队列，调用方就只能靠轮询看结果，
     参数写错了要等十几分钟才知道，体验极差。
  2. **同步数据层的线程池包装** —— :class:`~app.services.dm_store.DMStore` 是给
     Celery worker 写的同步实现（原因见该模块文档）。FastAPI 侧复用它可以避免
     维护两套 SQL，但**绝不能在事件循环里直接调**，否则一次网络往返就把整个
     worker 的事件循环卡住。所有调用一律走 ``run_in_threadpool``。
  3. **检索的向量化** —— 查询侧要加 BGE 指令前缀，文档侧不能加。这个不对称
     很容易搞错，统一收口在 :meth:`DMGuideService.search`，上层不用关心。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi.concurrency import run_in_threadpool

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigError, ConflictError, NotFoundError, ValidationError
from app.schemas.dm_guide import (
    AskRequest,
    AskResponse,
    AskSource,
    ChunkHit,
    DMGuideRef,
    DMGuideStatus,
    GuideQuestions,
    ImportPhase,
    ImportStatus,
    IngestRequest,
    IngestResponse,
    JobProgress,
    QAHit,
    QATitleChain,
    QATitleItem,
    QATitleNode,
    QuestionListResult,
    QuestionRecord,
    RetrievedHit,
    SearchResult,
)
from app.services import dm_store as store_mod
from app.services.repository import UploadTaskRepository
from app.services.script_service import slugify

logger = logging.getLogger("app.dm_service")

SEARCH_MODES = ("chunk", "qa", "hybrid")


def script_dm_code(script: Any) -> str:
    """按剧本中文名派生 DM 聚合 code。

    与 `scripts.code` 不完全等价：`scripts.code` 为了全表唯一可能带 `-2` 后缀，
    而 DM 需要把同名分片聚合到同一个业务 code 下，所以直接从标题生成基础 slug。
    """
    title = str(getattr(script, "title", "") or "").strip()
    script_id = str(getattr(script, "id", "") or "")
    return (slugify(title) or script_id).strip().lower()


# 问答（RAG 生成答案）的提示词。答案必须严格基于手册检索到的内容，
# 不编造 —— 主持人靠这个带本，瞎编一条规则就是事故。
_ANSWER_SYSTEM_PROMPT = """你是一名剧本杀主持人（DM）助手，专门依据《主持人手册》回答主持人带本过程中的问题。

硬性规则：
1. 只能使用下面【手册内容】里提供的参考来作答，严禁编造手册以外的任何规则、数字、人名、道具或页码。
2. 若【手册内容】不足以回答，明确说明「手册中未找到相关内容」，不要猜测或补全。
3. 回答要简洁、可直接照着执行，保留原文里的关键数字、时间、人名、道具名。
4. 用括号标注每条结论的出处，对应【手册内容】里的「参考N」，例如「（见参考2，P12）」。
5. 不要重复问题，直接给答案。"""

# 解析阶段到整体文案的映射，仅用于 import-status 的友好展示
_PARSE_STATUSES = {
    "pending",
    "downloading",
    "extracting",
    "chunking",
    "generating_qa",
    "embedding",
}
_TERMINAL_DONE = {"completed", "skipped"}


class DMGuideService:
    """DM 手册入库与检索的编排器。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

    @property
    def settings(self) -> Settings:
        return self._settings

    # --------------------------------------------------------
    # 守门
    # --------------------------------------------------------
    def _require_rag_config(self) -> None:
        """RAG 依赖多个外部服务，缺一个就跑不通。

        与其让任务派发出去后在 worker 里报一句看不见的错，
        不如在这里直接 500 并把缺失项列清楚 —— 部署时少一个环境变量太常见了。
        """
        missing = self._settings.missing_rag_config()
        if missing:
            raise ConfigError(
                "DM 指南解析未启用，缺少必要配置",
                code="dm_rag_disabled",
                details={"missing": missing},
            )

    def _resolve_ref(
        self, script: Any, override_key: Optional[str] = None
    ) -> DMGuideRef:
        """从剧本上解出 DM 手册的 OSS 定位信息。"""
        extra = getattr(script, "extra", None) or {}
        ref = DMGuideRef.from_extra(extra)

        if override_key:
            # 手动指定 objectKey 时，保留剧本上已有的文件名等元信息
            base = ref.model_dump() if ref else {}
            base["object_key"] = override_key
            base.pop("file_size", None)  # 换了文件，旧体积不再可信
            ref = DMGuideRef.model_validate(base)

        if not ref:
            raise ValidationError(
                "该剧本未关联 DM 主持人手册，请先上传 PDF / Word 并写入 extra.dmGuide",
                code="dm_guide_missing",
            )

        key = ref.object_key
        # 目前支持 PDF（走 PyMuPDF + 可选 OCR）与 Word(.docx)（python-docx 直接读文字层）。
        # 老版二进制 .doc 没有现成免费解析库，明确拒绝并提示另存为 .docx。
        lower = key.lower()
        if lower.endswith(".docx"):
            pass
        elif lower.endswith(".pdf"):
            pass
        elif lower.endswith(".doc"):
            # 旧版二进制 .doc 不再拒绝：流水线用本地 LibreOffice 无头转成 .docx 后照常解析，
            # 全程离线、不依赖任何付费 OCR / 转换服务。
            pass
        else:
            raise ValidationError(
                f"DM 手册目前只支持 PDF / Word(.doc/.docx)，收到：{key}",
                code="dm_guide_not_pdf",
            )

        limit = self._settings.dm_max_pdf_bytes
        if ref.file_size and limit and ref.file_size > limit:
            raise ValidationError(
                f"PDF 体积 {ref.file_size / 1024 / 1024:.1f}MB 超过上限 "
                f"{limit / 1024 / 1024:.0f}MB",
                code="dm_guide_too_large",
                details={"fileSize": ref.file_size, "limit": limit},
            )
        return ref

    # --------------------------------------------------------
    # 触发入库
    # --------------------------------------------------------
    async def trigger_ingest(
        self,
        script: Any,
        payload: Optional[IngestRequest] = None,
        *,
        user_id: str = "",
    ) -> IngestResponse:
        """校验并派发解析流水线。"""
        self._require_rag_config()
        payload = payload or IngestRequest()
        script_id = str(getattr(script, "id", "") or "")
        if not script_id:
            raise ValidationError("剧本 ID 缺失", code="script_id_required")
        script_code = script_dm_code(script)

        ref = self._resolve_ref(script, payload.object_key)
        store = store_mod.get_dm_store()

        # 同一剧本同时跑两条流水线没有意义：两边都会往同一批唯一约束上撞，
        # 后完成的那条大部分写入会被 on_conflict 吞掉，白烧一遍 API 额度。
        #
        # ★ 防重的粒度必须是「剧本实例 script_id」，绝不能按 script_code 查：
        #   同名剧本拆成的多个分片是不同的 script_id、共享同一个 script_code。
        #   若按 code 查，分片 B 导入时会把分片 A 正在跑的任务误判成
        #   「本剧本文件已更换」并取消掉 —— 后导入的分片杀掉先导入的流水线，
        #   先导入那份的 QA 就永远建不起来（表现为「后导入覆盖先导入」）。
        #   各分片的任务互不影响，检索层按 script_code 聚合，QA 全部保留。
        active = await run_in_threadpool(store.find_active_job, script_id)
        if active and not payload.force:
            active_key = active.get("object_key")
            # 文件已变更（用户删掉旧文件、换了一份新文件重传）：旧任务对应的是老文件，
            # 必须作废旧任务、按新文件重新派发，否则状态会一直停在旧文件上，
            # 新文件永远进不了流水线（这正是「删了 pdf 传了 docs，解析状态还是 pdf」的原因）。
            if active_key and ref.object_key and active_key != ref.object_key:
                logger.info(
                    "检测到文件已变更，取消旧解析任务 job=%s old=%s new=%s",
                    active.get("id"), active_key, ref.object_key,
                )
                await run_in_threadpool(
                    store.update_job,
                    str(active.get("id")),
                    {
                        "status": store_mod.JOB_CANCELLED,
                        "error_message": "文件已更换，旧解析任务被新文件取代",
                    },
                )
            else:
                return IngestResponse(
                    job_id=str(active.get("id")),
                    status=str(active.get("status") or store_mod.JOB_PENDING),
                    reused=True,
                    message="该剧本已有解析任务在执行，返回现有任务进度",
                )
        if active and payload.force:
            # force 语义是「以新任务为准」，旧任务标记取消，避免进度接口拿到两条在跑
            await run_in_threadpool(
                store.update_job,
                str(active.get("id")),
                {"status": store_mod.JOB_CANCELLED, "error_message": "被强制重跑任务取代"},
            )

        # 派发新任务前，先把不属于当前文件的其他激活文档下线：
        # 否则旧文件的文档仍是 is_active=true，get_status 会把它当成「最新」展示。
        if ref.object_key:
            await run_in_threadpool(
                store.deactivate_documents_not_matching,
                script_id, ref.object_key, script_code=script_code,
            )

        job_id = str(uuid.uuid4())
        await run_in_threadpool(
            store.create_job,
            {
                "id": job_id,
                "script_id": script_id,
                "script_code": script_code,
                "status": store_mod.JOB_PENDING,
                "stage_detail": "任务已入队，等待 worker 领取",
                "object_key": ref.object_key,
                "created_by": user_id or None,
            },
        )

        from app.tasks.dm_ingest import dispatch_pipeline

        try:
            dispatch_pipeline(
                job_id=job_id,
                script_id=script_id,
                script_code=script_code,
                object_key=ref.object_key,
                file_name=ref.file_name or "",
                file_id=ref.file_id or "",
                file_size=ref.file_size or 0,
                script_title=str(getattr(script, "title", "") or ""),
                force=payload.force,
                created_by=user_id,
            )
        except Exception as exc:  # noqa: BLE001 - broker 挂了要立刻告诉调用方
            logger.exception("派发 DM 解析流水线失败: %s", exc)
            await run_in_threadpool(
                store.fail_job, job_id, f"任务派发失败：{exc}"
            )
            raise ConflictError(
                "任务派发失败，请确认消息队列可用", code="dm_dispatch_failed"
            ) from exc

        logger.info("DM 解析任务已派发 job=%s script=%s key=%s", job_id, script_id, ref.object_key)
        return IngestResponse(
            job_id=job_id,
            status=store_mod.JOB_PENDING,
            reused=False,
            message="解析任务已入队，可轮询进度接口跟踪",
        )

    async def maybe_trigger(self, script: Any, *, user_id: str = "") -> Optional[str]:
        """保存剧本后的自动触发钩子，**任何异常都不得向上冒泡**。

        剧本保存和手册解析是两件事。手册没配、Key 没填、队列挂了，
        都不该让「改个剧本简介」这样的操作失败。所以这里吞掉全部异常，
        只留日志 —— 需要确定性结果的调用方走显式的 ingest 接口。
        """
        try:
            if not self._settings.dm_rag_enabled:
                return None
            if not DMGuideRef.from_extra(getattr(script, "extra", None)):
                return None

            script_id = str(getattr(script, "id", "") or "")
            store = store_mod.get_dm_store()

            # 已经建好索引且文件没换过：绝大多数情况不必重跑。
            # 但若是「退化索引」——建了文档却 0 块或 0 问答（例如上一轮 QA 生成阶段
            # 中途失败），必须自动补跑，否则就是用户说的「导入后不再处理」的卡死态。
            doc = await run_in_threadpool(store.get_active_document, script_id)
            ref = DMGuideRef.from_extra(getattr(script, "extra", None))
            if doc and ref and doc.get("object_key") == ref.object_key:
                total_chunks = int(doc.get("total_chunks") or 0)
                total_qa = int(doc.get("total_qa") or 0)
                if total_chunks > 0 and total_qa > 0:
                    return None
                logger.info(
                    "检测到退化索引(块=%s 问答=%s)，自动以 force 补跑解析 script=%s",
                    total_chunks, total_qa, script_id,
                )
                result = await self.trigger_ingest(
                    script, payload=IngestRequest(force=True), user_id=user_id
                )
                return result.job_id

            result = await self.trigger_ingest(script, user_id=user_id)
            return result.job_id
        except Exception as exc:  # noqa: BLE001 - 自动触发失败不影响剧本保存
            logger.warning(
                "自动触发 DM 解析失败（已忽略）script=%s: %s",
                getattr(script, "id", "?"),
                exc,
            )
            return None

    # --------------------------------------------------------
    # 进度与状态
    # --------------------------------------------------------
    @staticmethod
    def _to_progress(row: Dict[str, Any]) -> JobProgress:
        return JobProgress(
            job_id=str(row.get("id") or ""),
            script_id=str(row.get("script_id") or ""),
            document_id=(str(row["document_id"]) if row.get("document_id") else None),
            status=str(row.get("status") or store_mod.JOB_PENDING),
            stage_detail=row.get("stage_detail"),
            total_pages=int(row.get("total_pages") or 0),
            processed_pages=int(row.get("processed_pages") or 0),
            total_shards=int(row.get("total_shards") or 0),
            finished_shards=int(row.get("finished_shards") or 0),
            total_chunks=int(row.get("total_chunks") or 0),
            embedded_chunks=int(row.get("embedded_chunks") or 0),
            total_qa=int(row.get("total_qa") or 0),
            embedded_qa=int(row.get("embedded_qa") or 0),
            error_message=row.get("error_message"),
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
        )

    async def get_job(self, job_id: str) -> JobProgress:
        store = store_mod.get_dm_store()
        row = await run_in_threadpool(store.get_job, job_id)
        if not row:
            raise NotFoundError(f"任务不存在: {job_id}", code="dm_job_not_found")
        return self._to_progress(row)

    async def get_status(self, script: Any) -> DMGuideStatus:
        """剧本详情页用的聚合状态。"""
        script_id = str(getattr(script, "id", "") or "")
        script_code = script_dm_code(script)
        ref = DMGuideRef.from_extra(getattr(script, "extra", None))

        if not self._settings.dm_rag_enabled:
            return DMGuideStatus(
                script_id=script_id, has_guide=bool(ref), indexed=False
            )

        store = store_mod.get_dm_store()
        docs = await run_in_threadpool(store.list_active_documents_by_code, script_code)
        if not docs:
            doc = await run_in_threadpool(
                store.get_active_document, script_id, script_code=script_code
            )
            docs = [doc] if doc else []
        # 优先锚定到「当前关联文件」对应的文档：用户换文件重传后，库里可能同时残留
        # 旧文件的激活文档（且 created_at 更新），若只取「最新激活」会把旧文件当成
        # 当前手册，导致解析状态文件名卡在旧文件上。
        if ref and ref.object_key:
            matched = next(
                (d for d in docs if d and d.get("object_key") == ref.object_key), None
            )
            latest_doc = matched if matched is not None else (docs[0] if docs else None)
        else:
            latest_doc = docs[0] if docs else None
        job_row = await run_in_threadpool(
            store.latest_job, script_id, script_code=script_code
        )
        # 同理：若最新任务对应的是已被替换的旧文件，优先取当前文件的最新任务，
        # 避免「解析状态」展示成旧文件。
        if ref and ref.object_key and job_row and job_row.get("object_key") != ref.object_key:
            matched_job = await run_in_threadpool(
                store.latest_job, script_id, script_code=script_code, object_key=ref.object_key
            )
            if matched_job:
                job_row = matched_job

        total_pages = sum(int(d.get("total_pages") or 0) for d in docs)
        total_chunks = sum(int(d.get("total_chunks") or 0) for d in docs)
        total_qa = sum(int(d.get("total_qa") or 0) for d in docs)
        version = int(latest_doc.get("version") or 0) if latest_doc else 0
        indexed = bool(total_chunks > 0)

        return DMGuideStatus(
            script_id=script_id,
            has_guide=bool(ref),
            indexed=indexed,
            document_id=(str(latest_doc["id"]) if latest_doc else None),
            file_name=(latest_doc.get("file_name") if latest_doc else None) or (ref.file_name if ref else None),
            total_pages=total_pages,
            total_chunks=total_chunks,
            total_qa=total_qa,
            version=version,
            job=self._to_progress(job_row) if job_row else None,
        )

    async def get_import_status(
        self, script: Any, *, upload_task_id: Optional[str] = None
    ) -> ImportStatus:
        """剧本导入整体进度：上传手册 → 解析入库 → 可问答。

        前端上传完手册、保存剧本后即开始轮询这个接口即可，不必再分别盯
        `GET /uploads/{task_id}` 和解析进度接口。传 `upload_task_id` 可把
        传输中的实时字节进度也并入 `upload` 字段（传输通常只有几秒，不传也可）。
        """
        script_id = str(getattr(script, "id", "") or "")
        title = str(getattr(script, "title", "") or "") or None

        status = await self.get_status(script)

        # ---- 上传阶段 ----
        upload_detail: Optional[Dict[str, Any]] = None
        upload_status = "done" if status.has_guide else "pending"
        upload_progress = 100.0 if status.has_guide else 0.0
        if upload_task_id:
            repo = UploadTaskRepository()
            task = await run_in_threadpool(repo.get, upload_task_id)
            if task:
                prog = float(task.get("progress") or 0.0)
                upload_detail = {
                    "taskId": str(task.get("id")),
                    "status": task.get("status"),
                    "progress": round(prog, 1),
                    "uploadedBytes": int(task.get("uploaded_bytes") or 0),
                    "totalBytes": int(task.get("file_size") or 0),
                    "totalParts": int(task.get("total_parts") or 0),
                    "filename": task.get("filename"),
                }
                tstatus = task.get("status")
                if tstatus == "uploading":
                    upload_status, upload_progress = "active", prog
                elif tstatus == "failed":
                    upload_status, upload_progress = "failed", prog

        # ---- 解析阶段 ----
        job = status.job
        parse_status = "pending"
        parse_progress = 0.0
        parse_detail: Dict[str, Any] = {}
        if job is not None:
            parse_detail = job.model_dump(by_alias=True)
            jstatus = job.status
            if jstatus in _TERMINAL_DONE:
                parse_status, parse_progress = "done", 100.0
            elif jstatus == "failed":
                parse_status, parse_progress = "failed", 0.0
            elif jstatus == "cancelled":
                parse_status, parse_progress = "cancelled", 0.0
            else:
                # 向量化计数到达上限即视为解析完成：embed_and_store 每批都是在
                # chunk 与 QA 都入库后才累加 embedded_chunks，所以 embedded >= total
                # 时全部内容实际已写完，只差 finalize 落终态的一瞬；
                # 若仍报 active，前端会看到「进度 100% 却迟迟不完」。
                # 用 >= 兼容重试导致计数器 += 虚高（超过 total）的情况。
                if job.total_chunks > 0 and job.embedded_chunks >= job.total_chunks:
                    parse_status, parse_progress = "done", 100.0
                else:
                    parse_status = "active"
                    parse_progress = _coarse_progress(job)

        elif status.has_guide and status.indexed:
            parse_status, parse_progress = "done", 100.0

        # ---- 可问答阶段 ----
        ready_status = "done" if status.indexed else "pending"

        # ---- 整体状态 ----
        if upload_status == "active":
            overall = "uploading"
        elif upload_status == "failed":
            overall = "failed"
        elif not status.has_guide and upload_status != "active":
            overall = "no_guide"
        elif parse_status in ("active",):
            overall = "parsing"
        elif parse_status == "failed":
            overall = "failed"
        elif parse_status == "cancelled":
            overall = "pending"
        elif parse_status == "done":
            # 解析流水线已结束（job 终态 completed/skipped；或向量化计数到达上限；
            # 或 has_guide 且无 job 但已 indexed）。
            # indexed 为 False 多为「0 chunk 的退化完成」——仍需给前端明确的「解析结束」信号，
            # 不能落到 pending 与「未开始」混淆；用 parsed 区分「已解析完成、暂不可问答」。
            overall = "ready" if status.indexed else "parsed"
        elif status.indexed:
            overall = "ready"
        else:
            overall = "pending"

        phases = [
            ImportPhase(
                key="upload", label="上传手册", status=upload_status,
                progress=upload_progress, detail=upload_detail,
            ),
            ImportPhase(
                key="parse", label="解析入库", status=parse_status,
                progress=parse_progress, detail=parse_detail or None,
            ),
            ImportPhase(
                key="ready", label="可问答", status=ready_status,
                progress=100.0 if ready_status == "done" else 0.0,
            ),
        ]

        dm_guide = status.model_dump(by_alias=True)
        return ImportStatus(
            script_id=script_id,
            title=title,
            overall_status=overall,
            phases=phases,
            upload=upload_detail,
            dm_guide=dm_guide,
        )

    # --------------------------------------------------------
    # 检索
    # --------------------------------------------------------
    async def search(
        self,
        *,
        query: str,
        script_id: Optional[str] = None,
        script_code: Optional[str] = None,
        document_id: Optional[str] = None,
        mode: str = "hybrid",
        top_k: Optional[int] = None,
        min_similarity: Optional[float] = None,
        category: Optional[str] = None,
    ) -> SearchResult:
        """向量检索。

        `mode=hybrid` 时问答对与原文块各查一轮。两次 RPC 都要用同一条查询向量，
        所以向量化只做一次 —— embedding 是这里唯一的外部网络调用，
        重复一次就把 P99 翻倍。
        """
        self._require_rag_config()
        query = (query or "").strip()
        if not query:
            raise ValidationError("检索关键词不能为空", code="query_required")
        if mode not in SEARCH_MODES:
            raise ValidationError(
                f"不支持的检索模式: {mode}",
                code="invalid_search_mode",
                details={"allowed": list(SEARCH_MODES)},
            )

        top_k = top_k or self._settings.dm_search_top_k
        threshold = (
            min_similarity
            if min_similarity is not None
            else self._settings.dm_search_min_similarity
        )

        started = time.perf_counter()
        from app.services.llm import get_llm_client

        client = get_llm_client()
        # 查询侧要加 BGE 指令前缀，aembed_query 内部已处理，这里不要重复加
        vector = await client.aembed_query(query)

        store = store_mod.get_dm_store()
        chunks: List[Dict[str, Any]] = []
        qa: List[Dict[str, Any]] = []

        # hybrid 模式「以 qa 为主召回」：qa 取满 top_k 做主答案来源，
        # chunk 仅作出处佐证、配额收紧到 dm_search_qa_supplement_k。
        qa_k = top_k
        chunk_k = top_k
        if mode == "hybrid":
            qa_k = top_k
            chunk_k = min(top_k, self._settings.dm_search_qa_supplement_k)

        if mode in ("chunk", "hybrid"):
            chunks = await run_in_threadpool(
                store.match_chunks,
                vector,
                script_id=script_id,
                script_code=script_code,
                document_id=document_id,
                match_count=chunk_k,
                similarity_threshold=threshold,
            )
        if mode in ("qa", "hybrid"):
            qa = await run_in_threadpool(
                store.match_qa,
                vector,
                script_id=script_id,
                script_code=script_code,
                document_id=document_id,
                category=category,
                match_count=qa_k,
                similarity_threshold=threshold,
            )

        chunk_hits = [_to_chunk_hit(r) for r in chunks]
        qa_hits = [_to_qa_hit(r) for r in qa]
        hits = _merge_hits_qa_first(qa_hits, chunk_hits, self._settings.dm_search_qa_boost)

        took = int((time.perf_counter() - started) * 1000)
        return SearchResult(
            query=query,
            mode=mode,
            document_id=document_id,
            chunks=chunk_hits,
            qa=qa_hits,
            hits=hits,
            took_ms=took,
        )

    # --------------------------------------------------------
    # 问答（RAG 生成答案）
    # --------------------------------------------------------
    async def ask(
        self,
        *,
        question: str,
        script: Any,
        mode: str = "hybrid",
        top_k: int = 6,
        min_similarity: Optional[float] = None,
        category: Optional[str] = None,
        use_llm: bool = False,
        user_id: str = "",
    ) -> AskResponse:
        """检索手册相关内容，返回答案。

        速度优先：`use_llm=False`（默认）时**不调用大模型**——直接把向量检索到的
        最近命中（qa 答案或原文块）透出为 `answer`，只花一次 embedding + 向量查询的
        开销，端到端通常在几十毫秒级，零 LLM 额度消耗。

        `use_llm=True` 时走原 RAG 链路：用 LLM 把命中内容合成一条带引用出处的答案。

        与 :meth:`search` 的区别：search 只返回原始命中（前端自己挑），ask 额外把
        答案凝练成一条可直接照着念的文本，并标注出处来源 `sources`。

        低相似度处理：本次检索的最高原始相似度低于 `dm_ask_meaningful_similarity`
        时，透出的答案基本不可信。此时照常返回答案，但会把问题按剧本维度沉淀到
        用户提问库（等待真人解答），并在响应里置 `need_human_answer=True`。
        落库是 best-effort：失败只记日志，绝不影响问答主链路。
        """
        self._require_rag_config()
        script_id = str(getattr(script, "id", "") or "")
        if not script_id:
            raise ValidationError("剧本 ID 缺失", code="script_id_required")

        # 没索引就回答不了 —— 直接告诉前端先等解析完成，不要抛空答
        status = await self.get_status(script)
        if not status.indexed:
            raise ConflictError(
                "该剧本手册尚未完成索引，暂时无法问答",
                code="dm_not_indexed",
                details={"hasGuide": status.has_guide, "indexed": False},
            )

        started = time.perf_counter()
        result = await self.search(
            query=question,
            script_id=script_id,
            script_code=script_dm_code(script),
            mode=mode,
            top_k=top_k,
            min_similarity=min_similarity,
            category=category,
        )

        # 组装引用来源：qa 优先，控制单轮上下文体量（避免把整本手册喂给 LLM）
        qa_cap = min(len(result.qa), top_k)
        chunk_cap = min(len(result.chunks), max(1, top_k - qa_cap))
        sources: List[AskSource] = []
        for h in result.qa[:qa_cap]:
            sources.append(
                AskSource(
                    type="qa",
                    similarity=h.similarity,
                    question=h.question,
                    answer=h.answer,
                    section_path=h.section_path,
                    page_start=h.page_start,
                    page_end=h.page_end,
                )
            )
        for h in result.chunks[:chunk_cap]:
            sources.append(
                AskSource(
                    type="chunk",
                    similarity=h.similarity,
                    content=h.content,
                    section_path=h.section_path,
                    page_start=h.page_start,
                    page_end=h.page_end,
                )
            )

        if use_llm:
            # LLM 合成路径：把命中内容揉成一句直接能照着念、带括号出处的答案。
            context = _format_answer_context(sources)
            user_prompt = (
                f"剧本：《{getattr(script, 'title', '') or script_id}》\n\n"
                f"【手册内容】\n{context}\n\n"
                f"【问题】\n{question}\n\n"
                "请基于手册内容回答上面的问题。"
            )
            from app.services.llm import get_llm_client

            client = get_llm_client()
            # chat 是同步阻塞网络调用，丢进线程池避免卡住事件循环
            answer = await run_in_threadpool(
                client.chat,
                [
                    {"role": "system", "content": _ANSWER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                **{"temperature": 0.3, "max_tokens": 1024},
            )
            answer = (answer or "").strip()
        else:
            # 速度最优路径：直接返回向量最近命中的答案文本，不调 LLM。
            answer = _compose_direct_answer(sources)

        took = int((time.perf_counter() - started) * 1000)

        # 最高原始相似度（qa 与 chunk 一起比，不带 boost 加权）：
        # 它是「这次检索到底有没有命中相关内容」的唯一可信判据。
        best_similarity = max(
            [h.similarity for h in result.qa] + [h.similarity for h in result.chunks],
            default=0.0,
        )
        need_human = best_similarity < self._settings.dm_ask_meaningful_similarity
        if need_human:
            await self._record_low_similarity_question(
                script=script,
                question=question,
                best_similarity=best_similarity,
                user_id=user_id,
            )

        return AskResponse(
            question=question,
            answer=answer,
            sources=sources,
            mode=result.mode,
            document_id=result.document_id,
            took_ms=took,
            best_similarity=round(best_similarity, 4),
            need_human_answer=need_human,
        )

    async def _record_low_similarity_question(
        self,
        *,
        script: Any,
        question: str,
        best_similarity: float,
        user_id: str = "",
    ) -> None:
        """把低相似度问题沉淀到提问库，**任何异常都不得向上冒泡**。

        问答是带本现场的高频链路：提问库写挂了（表未建、网络抖动）绝不能
        让主持人连答案都拿不到，所以这里吞掉全部异常、只留日志。
        """
        try:
            from app.services.dedup import content_hash

            store = store_mod.get_dm_store()
            await run_in_threadpool(
                store.record_question,
                script_id=str(getattr(script, "id", "") or ""),
                script_code=script_dm_code(script),
                question=question[:500],
                question_hash=content_hash(question),
                best_similarity=best_similarity,
                created_by=user_id or None,
            )
        except Exception as exc:  # noqa: BLE001 - 落库失败不影响问答主链路
            logger.warning("低相似度问题落库失败（已忽略）script=%s: %s",
                           getattr(script, "id", "?"), exc)

    # --------------------------------------------------------
    # 用户提问库（真人解答 + 引导问题）
    # --------------------------------------------------------
    async def list_questions(
        self,
        *,
        script_code: str,
        status_filter: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> QuestionListResult:
        """按剧本维度列出用户提问，供真人（主持人/运营）浏览与解答。"""
        code = (script_code or "").strip().lower()
        if not code:
            raise ValidationError("剧本标识缺失", code="script_code_required")
        if status_filter and status_filter not in store_mod.QUESTION_STATES:
            raise ValidationError(
                f"不支持的状态: {status_filter}",
                code="invalid_question_status",
                details={"allowed": sorted(store_mod.QUESTION_STATES)},
            )
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        store = store_mod.get_dm_store()
        rows, total = await run_in_threadpool(
            store.list_questions,
            script_code=code,
            status_filter=status_filter,
            limit=limit,
            offset=offset,
        )
        items = [_to_question_record(r) for r in rows]
        await self._attach_profiles(items)
        return QuestionListResult(total=total, items=items)

    async def answer_question(
        self, question_id: str, *, answer: str, answered_by: str
    ) -> QuestionRecord:
        """真人解答一条提问。解答后该问题可带答案透出为引导问题。"""
        store = store_mod.get_dm_store()
        existing = await run_in_threadpool(store.get_question, question_id)
        if not existing:
            raise NotFoundError(f"提问记录不存在: {question_id}", code="dm_question_not_found")
        if existing.get("status") == store_mod.QUESTION_DISMISSED:
            raise ConflictError(
                "该问题已被标记为无效，无法解答",
                code="dm_question_dismissed",
            )
        row = await run_in_threadpool(
            store.answer_question,
            question_id,
            answer=answer,
            answered_by=answered_by or None,
        )
        if not row:
            raise NotFoundError(f"提问记录不存在: {question_id}", code="dm_question_not_found")
        record = _to_question_record(row)
        await self._attach_profiles([record])
        return record

    async def guide_questions(
        self, *, script_code: str, script_title: str = "", limit: Optional[int] = None
    ) -> GuideQuestions:
        """剧本维度的引导问题：用户真实提问中人气最高的前 N 条。"""
        code = (script_code or "").strip().lower()
        if not code:
            raise ValidationError("剧本标识缺失", code="script_code_required")
        top = limit or self._settings.dm_guide_questions_limit
        top = max(1, min(top, 10))
        store = store_mod.get_dm_store()
        rows = await run_in_threadpool(store.list_guide_questions, code, limit=top)
        items = [_to_question_record(r) for r in rows]
        await self._attach_profiles(items)
        return GuideQuestions(
            script_code=code,
            script_title=script_title or None,
            items=items,
        )

    async def _attach_profiles(self, records: List[QuestionRecord]) -> None:
        """读取时批量关联 profiles，把最新昵称/头像合并进记录。

        提问记录只存 user id：资料以 profiles 表为唯一事实源，读取时一次
        ``id=in.(...)`` 批量查询覆盖整页 —— 头像改了下次读就是新的，
        不需要冗余快照，也不需要任何异步同步。

        profiles 查询失败只记日志、按无资料返回，绝不拖垮提问列表本身。
        """
        ids: List[str] = []
        for r in records:
            if r.created_by:
                ids.append(r.created_by)
            if r.answered_by:
                ids.append(r.answered_by)
        if not ids:
            return
        try:
            store = store_mod.get_dm_store()
            profiles = await run_in_threadpool(store.get_profiles, ids)
        except Exception as exc:  # noqa: BLE001 - 资料合并不影响提问主数据返回
            logger.warning("批量读取 profiles 失败（按无资料返回）: %s", exc)
            return
        _merge_profiles(records, profiles)

    # --------------------------------------------------------
    # 标题链（QA 按手册标题分组）
    # --------------------------------------------------------
    async def qa_title_chain(
        self, *, script_code: str, script_title: str = ""
    ) -> QATitleChain:
        """按剧本业务 code 取出全部问答，按手册标题组装成嵌套标题链。

        与向量检索不同，这里**不做语义匹配、不调 embedding**：
        就是把该剧本（含同名分片）已入库的 QA 按行文顺序全量列出、按标题分组，
        供前端做「玩家问答目录 + 标题下问答」的结构化浏览（QA 生成本身即面向玩家撰写）。
        """
        code = (script_code or "").strip().lower()
        if not code:
            raise ValidationError("剧本标识缺失，无法定位标题链", code="script_code_required")

        store = store_mod.get_dm_store()
        rows = await run_in_threadpool(store.list_qa_titles, code)
        titles, total_titles, total_qa = _build_title_tree(rows)
        return QATitleChain(
            script_code=code,
            script_title=script_title or None,
            total_titles=total_titles,
            total_qa=total_qa,
            titles=titles,
        )


# 没有章节信息的问答统一归入这个节点，避免前端收到空标题
_UNTITLED_NODE = "未分节"


def _build_title_tree(rows: List[Dict[str, Any]]) -> Tuple[List[QATitleNode], int, int]:
    """把 list_dm_qa_titles 的扁平行组装成嵌套标题树。

    入参行序必须是手册行文顺序（SQL 函数已保证），这里严格按首次出现顺序
    建树，不做二次排序。返回 (根节点列表, 叶子标题数, 问答总数)。

    层级来源是 section_path（章节面包屑）：逐前缀补出中间节点，
    QA 挂到完整路径对应的叶节点上；同一路径的多条 QA 自然聚合在同一标题下。
    """
    roots: List[Dict[str, Any]] = []
    index: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    total_qa = 0

    def ensure_node(path: List[str]) -> Dict[str, Any]:
        for i in range(1, len(path) + 1):
            key = tuple(path[:i])
            if key not in index:
                node: Dict[str, Any] = {
                    "title": path[i - 1],
                    "path": list(key),
                    "qa": [],
                    "children": [],
                }
                index[key] = node
                parent = index.get(tuple(key[:-1]))
                (parent["children"] if parent is not None else roots).append(node)
        return index[tuple(path)]

    for row in rows:
        raw_path = row.get("section_path")
        if isinstance(raw_path, str):
            # 与 _to_qa_hit 同款兼容：历史脏数据可能是逗号串
            path = [p for p in (s.strip() for s in raw_path.split(",")) if p]
        else:
            path = [str(p).strip() for p in (raw_path or []) if str(p).strip()]
        if not path:
            path = [_UNTITLED_NODE]
        node = ensure_node(path)
        node["qa"].append(
            QATitleItem(
                id=str(row.get("qa_id") or row.get("id") or ""),
                question=str(row.get("question") or ""),
                answer=str(row.get("answer") or ""),
                category=str(row.get("category") or "other"),
                page_start=row.get("page_start"),
                page_end=row.get("page_end"),
            )
        )
        total_qa += 1

    def to_model(node: Dict[str, Any]) -> QATitleNode:
        return QATitleNode(
            title=node["title"],
            path=node["path"],
            qa_count=len(node["qa"]),
            qa=node["qa"],
            children=[to_model(c) for c in node["children"]],
        )

    leaf_titles = sum(1 for n in index.values() if n["qa"])
    return [to_model(n) for n in roots], leaf_titles, total_qa


def _to_chunk_hit(row: Dict[str, Any]) -> ChunkHit:
    raw_path = row.get("section_path")
    if isinstance(raw_path, str):
        # PostgREST 对 text[] 返回 JSON 数组，但历史数据里可能是逗号串
        section_path = [p for p in (s.strip() for s in raw_path.split(",")) if p]
    else:
        section_path = list(raw_path or [])
    return ChunkHit(
        id=str(row.get("id") or ""),
        document_id=str(row.get("document_id") or ""),
        content=str(row.get("content") or ""),
        section_path=section_path,
        page_start=int(row.get("page_start") or 0),
        page_end=int(row.get("page_end") or 0),
        similarity=round(float(row.get("similarity") or 0.0), 4),
    )


def _to_qa_hit(row: Dict[str, Any]) -> QAHit:
    # section_path 在 DB 里是 text[]，PostgREST 正常返回 JSON 数组；
    # 但历史脏数据可能是逗号串，这里兼容一下，与 _to_chunk_hit 保持一致。
    raw_path = row.get("section_path")
    if isinstance(raw_path, str):
        section_path = [p for p in (s.strip() for s in raw_path.split(",")) if p]
    else:
        section_path = list(raw_path or [])
    return QAHit(
        id=str(row.get("id") or ""),
        document_id=str(row.get("document_id") or ""),
        question=str(row.get("question") or ""),
        answer=str(row.get("answer") or ""),
        category=row.get("category"),
        chunk_id=(str(row["chunk_id"]) if row.get("chunk_id") else None),
        section_path=section_path,
        page_start=int(row.get("page_start") or 0),
        page_end=int(row.get("page_end") or 0),
        similarity=round(float(row.get("similarity") or 0.0), 4),
    )


def _to_question_record(row: Dict[str, Any]) -> QuestionRecord:
    """数据库行 → 提问记录。昵称/头像不在行里，由 _merge_profiles 读取时合并。"""
    return QuestionRecord(
        id=str(row.get("id") or ""),
        script_id=str(row.get("script_id") or ""),
        script_code=str(row.get("script_code") or ""),
        question=str(row.get("question") or ""),
        ask_count=int(row.get("ask_count") or 1),
        best_similarity=round(float(row.get("best_similarity") or 0.0), 4),
        status=str(row.get("status") or store_mod.QUESTION_PENDING),
        answer=row.get("answer"),
        answered_by=(str(row["answered_by"]) if row.get("answered_by") else None),
        answered_at=row.get("answered_at"),
        created_by=(str(row["created_by"]) if row.get("created_by") else None),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _merge_profiles(
    records: List[QuestionRecord], profiles: Dict[str, Dict[str, Any]]
) -> None:
    """把 profiles 行按 user id 合并进提问记录的展示字段（原地修改）。

    纯函数、无副作用，便于单测：合并逻辑与「怎么取到 profiles」解耦。
    资料为空的用户保持 None，前端按渐变头像兜底。
    """
    for r in records:
        creator = profiles.get(r.created_by or "")
        if creator:
            r.created_by_nickname = creator.get("nickname")
            r.created_by_avatar_url = creator.get("avatar_url")
            r.created_by_avatar_color = creator.get("avatar_color")
        answerer = profiles.get(r.answered_by or "")
        if answerer:
            r.answered_by_nickname = answerer.get("nickname")
            r.answered_by_avatar_url = answerer.get("avatar_url")
            r.answered_by_avatar_color = answerer.get("avatar_color")


def _coarse_progress(job: "JobProgress") -> float:
    """把多阶段解析进度收敛成一个 0~100 的粗进度，仅供进度条展示。

    四个阶段耗时差两个数量级，单一百分比本不科学（见 JobProgress 文档），
    但 import-status 需要一个能填进度条的概数；这里按「哪个计数先到顶」取最大，
    绝不靠线性插值骗用户。
    """
    if job.total_chunks > 0:
        return min(100.0, round(job.embedded_chunks / job.total_chunks * 100, 1))
    if job.total_qa > 0:
        return min(100.0, round(job.embedded_qa / job.total_qa * 100, 1))
    if job.total_pages > 0:
        return min(100.0, round(job.processed_pages / job.total_pages * 100, 1))
    if job.total_shards > 0:
        return min(100.0, round(job.finished_shards / job.total_shards * 100, 1))
    return 0.0


def _compose_direct_answer(
    sources: List[AskSource], fallback: str = "手册中未找到相关内容"
) -> str:
    """速度最优路径：直接返回向量最近命中的答案文本，不调用 LLM。

    `sources` 在 :meth:`DMGuideService.ask` 内已是 **qa 优先** 排序（先放 qa 命中、
    再放 chunk 命中），所以取首条即可得到本次检索中业务权重最高、且相似度最靠前的那条；
    对 chunk 模式则取相似度最高的原文块。无需任何模型推理，仅字符串拼接。
    """
    if not sources:
        return fallback
    top = sources[0]
    if top.type == "qa":
        return (top.answer or "").strip()
    return (top.content or "").strip()


def _format_answer_context(sources: List[AskSource]) -> str:
    """把引用来源拼成 LLM 能读懂的【手册内容】文本。"""
    blocks: List[str] = []
    for i, s in enumerate(sources, 1):
        path = " > ".join(s.section_path) if s.section_path else ""
        page = ""
        if s.page_start:
            page = f" P{s.page_start}"
            if s.page_end and s.page_end != s.page_start:
                page += f"-{s.page_end}"
        where = (f"（{path}{page}）" if (path or page) else "")
        if s.type == "qa":
            blocks.append(f"[参考{i}]{where}\n问：{s.question}\n答：{s.answer}")
        else:
            blocks.append(f"[参考{i}]{where}\n{s.content or ''}")
    return "\n\n".join(blocks)


def _merge_hits_qa_first(
    qa_hits: List[QAHit],
    chunk_hits: List[ChunkHit],
    boost: float,
) -> List[RetrievedHit]:
    """合并成「qa 优先」的扁平召回视图。

    qa 的排序分 = 原始相似度 * boost，使其在混合后稳定排在 chunk 之前；
    chunk 用原始相似度。最终整体按排序分降序，同分 qa 在前。
    """
    items: List[Tuple[float, RetrievedHit]] = []
    for h in qa_hits:
        score = round(h.similarity * boost, 4)
        items.append((score, RetrievedHit(
            type="qa",
            similarity=score,
            raw_similarity=h.similarity,
            payload=h.model_dump(by_alias=True),
        )))
    for h in chunk_hits:
        items.append((h.similarity, RetrievedHit(
            type="chunk",
            similarity=h.similarity,
            raw_similarity=h.similarity,
            payload=h.model_dump(by_alias=True),
        )))
    items.sort(key=lambda x: x[0], reverse=True)
    return [hit for _, hit in items]


_service: Optional[DMGuideService] = None


def get_dm_guide_service() -> DMGuideService:
    """FastAPI 依赖注入入口。"""
    global _service
    if _service is None:
        _service = DMGuideService()
    return _service


def reset_dm_guide_service() -> None:
    global _service
    _service = None
