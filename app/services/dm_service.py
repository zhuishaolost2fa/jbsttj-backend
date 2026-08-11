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
    ImportPhase,
    ImportStatus,
    IngestRequest,
    IngestResponse,
    JobProgress,
    QAHit,
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
        active = await run_in_threadpool(
            store.find_active_job, script_id, script_code=script_code
        )
        if active and not payload.force:
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

            # 已经建好索引且文件没换过，就不必重跑
            doc = await run_in_threadpool(store.get_active_document, script_id)
            ref = DMGuideRef.from_extra(getattr(script, "extra", None))
            if doc and ref and doc.get("object_key") == ref.object_key:
                return None

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
        latest_doc = docs[0] if docs else None
        job_row = await run_in_threadpool(
            store.latest_job, script_id, script_code=script_code
        )

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
    ) -> AskResponse:
        """检索手册相关内容，再用 LLM 合成一条带引用的答案。

        与 :meth:`search` 的区别：search 只返回原始命中（前端自己挑），
        ask 额外走一步 LLM 把命中内容揉成一句直接能照着念的答案，并标注出处。
        答案**严格**基于检索到的手册内容，模型无法看到的页面不会出现在答案里。
        """
        self._require_rag_config()
        script_id = str(getattr(script, "id", "") or "")
        if not script_id:
            raise ValidationError("剧本 ID 缺失", code="script_id_required")

        # 没索引就回答不了 —— 直接告诉前端先等解析完成，不要抛 LLM 空答
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

        took = int((time.perf_counter() - started) * 1000)
        return AskResponse(
            question=question,
            answer=(answer or "").strip(),
            sources=sources,
            mode=result.mode,
            document_id=result.document_id,
            took_ms=took,
        )


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
    return QAHit(
        id=str(row.get("id") or ""),
        document_id=str(row.get("document_id") or ""),
        question=str(row.get("question") or ""),
        answer=str(row.get("answer") or ""),
        category=row.get("category"),
        chunk_id=(str(row["chunk_id"]) if row.get("chunk_id") else None),
        similarity=round(float(row.get("similarity") or 0.0), 4),
    )


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
