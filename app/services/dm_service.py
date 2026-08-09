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
    ChunkHit,
    DMGuideRef,
    DMGuideStatus,
    IngestRequest,
    IngestResponse,
    JobProgress,
    QAHit,
    RetrievedHit,
    SearchResult,
)
from app.services import dm_store as store_mod

logger = logging.getLogger("app.dm_service")

SEARCH_MODES = ("chunk", "qa", "hybrid")


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
                "该剧本未关联 DM 主持人手册，请先上传 PDF 并写入 extra.dmGuide",
                code="dm_guide_missing",
            )

        key = ref.object_key
        if not key.lower().endswith(".pdf"):
            raise ValidationError(
                f"DM 手册目前只支持 PDF，收到：{key}", code="dm_guide_not_pdf"
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

        ref = self._resolve_ref(script, payload.object_key)
        store = store_mod.get_dm_store()

        # 同一剧本同时跑两条流水线没有意义：两边都会往同一批唯一约束上撞，
        # 后完成的那条大部分写入会被 on_conflict 吞掉，白烧一遍 API 额度。
        active = await run_in_threadpool(store.find_active_job, script_id)
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
        ref = DMGuideRef.from_extra(getattr(script, "extra", None))

        if not self._settings.dm_rag_enabled:
            return DMGuideStatus(
                script_id=script_id, has_guide=bool(ref), indexed=False
            )

        store = store_mod.get_dm_store()
        doc = await run_in_threadpool(store.get_active_document, script_id)
        job_row = await run_in_threadpool(store.latest_job, script_id)

        return DMGuideStatus(
            script_id=script_id,
            has_guide=bool(ref),
            indexed=bool(doc and int(doc.get("total_chunks") or 0) > 0),
            document_id=(str(doc["id"]) if doc else None),
            file_name=(doc.get("file_name") if doc else None) or (ref.file_name if ref else None),
            total_pages=int((doc or {}).get("total_pages") or 0),
            total_chunks=int((doc or {}).get("total_chunks") or 0),
            total_qa=int((doc or {}).get("total_qa") or 0),
            version=int((doc or {}).get("version") or 0),
            job=self._to_progress(job_row) if job_row else None,
        )

    # --------------------------------------------------------
    # 检索
    # --------------------------------------------------------
    async def search(
        self,
        *,
        query: str,
        script_id: Optional[str] = None,
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
                document_id=document_id,
                match_count=chunk_k,
                similarity_threshold=threshold,
            )
        if mode in ("qa", "hybrid"):
            qa = await run_in_threadpool(
                store.match_qa,
                vector,
                script_id=script_id,
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
