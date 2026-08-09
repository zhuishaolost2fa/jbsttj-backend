"""DM 指南入库流水线：四任务并行编排。

::

    prepare_document (T0)
        │  下载 PDF → 内容指纹 → 建文档记录 → 规划分片
        ▼
    ┌── chord ────────────────────────────────────────────┐
    │  group( extract_shard × N )        ← T1  CPU 密集    │
    └────────────────────┬────────────────────────────────┘
                         ▼
              chunk_and_dedup (T2)   ← 单任务，全局视野
                         │  合并分片 → 剥页眉页脚 → 语义分块 → 全局去重
                         ▼
    ┌── chord ────────────────────────────────────────────┐
    │  group(                                              │
    │     chain( generate_qa(批1) → embed_and_store(批1) ),│
    │     chain( generate_qa(批2) → embed_and_store(批2) ),│  ← T3 → T4
    │     ...                                              │
    │  )                                                   │
    └────────────────────┬────────────────────────────────┘
                         ▼
                    finalize

**T3 与 T4 为什么用 chain 而不是两个 chord？**

如果写成 ``chord(所有 T3) → chord(所有 T4)``，那么最后一批 QA 生成完之前，
第一批的向量化一秒都动不了 —— 流水线退化成「全部生成 → 全部向量化」两个串行大阶段，
总耗时 = max(T3) + max(T4)。

改成每批各自 ``chain(T3 → T4)`` 后，批 1 在生成 QA 的同时批 2 已经在向量化，
两个阶段真正重叠起来，总耗时 ≈ max(单批 T3 + 单批 T4)。
400 页手册按 6 块一批能切出上百批，这个重叠收益相当可观。

**幂等性**

``task_acks_late=True`` 意味着 worker 崩溃时任务会重投，所以每个任务都必须幂等：

  - T1 纯函数，重跑无副作用；
  - T2 的去重指纹带文档命名空间，重跑得到同样的 chunk 集合；
  - T4 依赖 ``(document_id, content_hash)`` 唯一约束，重复写入被数据库吞掉。
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.config import get_settings
from app.core.exceptions import AppError, DatabaseError, LLMError, StorageError
from app.services import dm_store as store_mod
from app.services.chunking import Chunk, ChunkConfig, chunk_blocks, chunks_from_payload
from app.services.dedup import Deduplicator, build_backend, to_signed_64
from app.services.dm_store import (
    JOB_CHUNKING,
    JOB_COMPLETED,
    JOB_DOWNLOADING,
    JOB_EMBEDDING,
    JOB_EXTRACTING,
    JOB_GENERATING_QA,
    JOB_SKIPPED,
    get_dm_store,
    to_pgvector,
)
from app.services.llm import QAPair, get_llm_client
from app.services.pdf_extract import (
    ShardResult,
    build_section_paths,
    calibrate_headings,
    merge_shards,
    plan_shards,
    probe_page_count,
    strip_noise,
)
from app.tasks.celery_app import celery_app, celery_available

logger = logging.getLogger("app.tasks.dm")


# ============================================================
# 任务装饰器：Celery 缺失时降级为普通函数
# ============================================================
def _task(**opts: Any):
    """注册 Celery 任务；未安装 celery 时保留原函数，便于离线单测直接调用。"""

    def decorator(fn):
        if not celery_available():
            fn.is_celery_task = False
            return fn
        wrapped = celery_app.task(**opts)(fn)
        wrapped.is_celery_task = True
        return wrapped

    return decorator


_RETRY_OPTS: Dict[str, Any] = {
    "autoretry_for": (StorageError, DatabaseError, LLMError, OSError),
    "retry_backoff": 4,
    "retry_backoff_max": 300,
    "retry_jitter": True,
    "max_retries": 3,
}


# ============================================================
# 本地 PDF 缓存
# ============================================================
def _cache_dir() -> Path:
    path = Path(get_settings().dm_cache_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_name(object_key: str) -> str:
    """用 object_key 的摘要做缓存文件名。

    直接拿 object_key 当文件名会踩两个坑：里面有 `/` 会被当成目录，
    中文文件名在不同 worker 的 locale 下编码还可能不一致。
    """
    return hashlib.sha1(object_key.encode("utf-8")).hexdigest() + ".pdf"


def sha256_file(path: str, *, chunk_size: int = 1 << 20) -> str:
    """流式计算文件 SHA256（不把上百 MB 的 PDF 读进内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def ensure_local_pdf(object_key: str, *, expected_size: int = 0) -> Tuple[str, int]:
    """确保 PDF 已在本地，返回 (路径, 字节数)。

    T0 和每个 T1 都会调用它。同机多 worker 时第一个把文件拉下来，
    其余直接命中缓存；跨机部署时各机器各下一份，逻辑无需区分。

    并发下载用「临时文件 + 原子重命名」避免读到写了一半的文件 ——
    PyMuPDF 读到截断的 PDF 会抛出难以定位的解析错误。
    """
    settings = get_settings()
    target = _cache_dir() / _cache_name(object_key)

    if target.exists():
        size = target.stat().st_size
        # 有 expected_size 时校验，防止上次下载中断留下的残缺文件被当成有效缓存
        if size > 0 and (not expected_size or size == expected_size):
            logger.debug("命中本地 PDF 缓存: %s (%s 字节)", target, size)
            return str(target), size
        logger.warning("本地缓存大小异常(%s != %s)，重新下载", size, expected_size)
        target.unlink(missing_ok=True)

    if expected_size and expected_size > settings.dm_max_pdf_bytes:
        raise StorageError(
            f"PDF 体积 {expected_size} 字节超过上限 {settings.dm_max_pdf_bytes} 字节"
        )

    from app.services.oss import get_oss_service

    tmp_path = target.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.part")
    started = time.time()
    try:
        size = get_oss_service().download_to_file_sync(object_key, str(tmp_path))
        if size > settings.dm_max_pdf_bytes:
            raise StorageError(f"PDF 体积 {size} 字节超过上限")
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    logger.info(
        "PDF 下载完成: %s (%.1f MB, 耗时 %.1fs)",
        object_key, size / 1024 / 1024, time.time() - started,
    )
    return str(target), size


# ============================================================
# T0：准备文档并派发提取
# ============================================================
@_task(bind=True, name="dm.prepare_document", **_RETRY_OPTS)
def prepare_document(
    self,
    job_id: str,
    script_id: str,
    object_key: str,
    *,
    file_name: str = "",
    file_id: str = "",
    file_size: int = 0,
    script_title: str = "",
    force: bool = False,
) -> Dict[str, Any]:
    """下载 PDF、建文档记录、规划分片并派发 T1 群组。"""
    store = get_dm_store()
    settings = get_settings()

    request = getattr(self, "request", None)
    store.update_job(
        job_id,
        {
            "status": JOB_DOWNLOADING,
            "stage_detail": "正在从 OSS 下载 PDF",
            "started_at": "now()",
            "celery_task_id": getattr(request, "id", None),
        },
    )

    local_path, size = ensure_local_pdf(object_key, expected_size=file_size)
    content_hash = sha256_file(local_path)
    total_pages = probe_page_count(local_path)
    if total_pages <= 0:
        raise StorageError("PDF 页数为 0，文件可能已损坏")

    document = store.upsert_document(
        {
            "script_id": script_id,
            "file_id": file_id or None,
            "object_key": object_key,
            "file_name": file_name or None,
            "file_size": size,
            "content_hash": content_hash,
            "total_pages": total_pages,
            "version": store.next_version(script_id),
            "is_active": True,
            "embed_model": settings.siliconflow_embed_model,
            "chat_model": settings.siliconflow_chat_model,
        }
    )
    document_id = str(document["id"])

    # ---- 幂等短路 ----------------------------------------------------
    # 内容指纹是幂等键的意义就在这里：同一份 PDF 换个 object_key 重传、
    # 或者前端重复点了两次「解析」，不该再烧一遍完整流水线。
    # 400 页手册跑一轮是几千次 embedding 加上百次 LLM 调用，省下来是实打实的钱和时间。
    #
    # 判据用 document.total_chunks（由 finalize 回写）而不是直接数 chunks 表：
    # 上一轮如果在向量化阶段崩了一半，chunks 表里也有几百行，但索引是残缺的，
    # 这时必须重跑。只有 finalize 真正执行过，total_chunks 才会是非零值。
    if not force:
        declared = int(document.get("total_chunks") or 0)
        if declared > 0 and store.count_chunks(document_id) >= declared:
            store.update_document(document_id, {"is_active": True})
            store.deactivate_other_versions(script_id, document_id)
            store.update_job(
                job_id,
                {
                    "status": JOB_SKIPPED,
                    "document_id": document_id,
                    "total_pages": total_pages,
                    "total_chunks": declared,
                    "total_qa": int(document.get("total_qa") or 0),
                    "stage_detail": "内容指纹命中已完成的版本，复用现有索引",
                    "finished_at": "now()",
                },
            )
            logger.info(
                "命中幂等，跳过解析 doc=%s hash=%s chunks=%s",
                document_id, content_hash[:12], declared,
            )
            return {
                "document_id": document_id,
                "total_pages": total_pages,
                "shards": [],
                "dispatched": False,
                "skipped": True,
            }

    # force 重跑要的是「换了模型/换了分块参数后重新生成」，必须先清干净，
    # 否则唯一约束会让新参数切出来的块全部被 on_conflict 吞掉
    if force:
        logger.info("force=True，清空文档 %s 的历史 chunk 与 QA", document_id)
        store.purge_document(document_id)

    shards = plan_shards(total_pages, settings.dm_extract_pages_per_shard)
    store.update_job(
        job_id,
        {
            "status": JOB_EXTRACTING,
            "document_id": document_id,
            "total_pages": total_pages,
            "total_shards": len(shards),
            "stage_detail": f"共 {total_pages} 页，切分为 {len(shards)} 个提取分片",
        },
    )
    logger.info(
        "文档就绪 doc=%s pages=%s shards=%s hash=%s",
        document_id, total_pages, len(shards), content_hash[:12],
    )

    if not celery_available():
        return {
            "document_id": document_id,
            "total_pages": total_pages,
            "shards": shards,
            "dispatched": False,
        }

    from celery import chord

    header = [
        extract_shard.s(
            object_key=object_key,
            shard_index=i,
            page_start=start,
            page_end=end,
            file_size=size,
            job_id=job_id,
        )
        for i, (start, end) in enumerate(shards)
    ]
    callback = chunk_and_dedup.s(
        job_id=job_id,
        document_id=document_id,
        script_id=script_id,
        script_title=script_title,
        object_key=object_key,
    )
    chord(header)(callback.on_error(on_pipeline_error.s(job_id=job_id)))

    return {
        "document_id": document_id,
        "total_pages": total_pages,
        "total_shards": len(shards),
        "dispatched": True,
    }


# ============================================================
# T1：分片提取（CPU 密集，可水平扩展）
# ============================================================
@_task(bind=True, name="dm.extract_shard", **_RETRY_OPTS)
def extract_shard(
    self,
    *,
    object_key: str,
    shard_index: int,
    page_start: int,
    page_end: int,
    file_size: int = 0,
    job_id: str = "",
) -> Dict[str, Any]:
    """提取 [page_start, page_end] 的结构化文本，返回可 JSON 化的分片结果。"""
    from app.services.pdf_extract import extract_shard as do_extract

    local_path, _ = ensure_local_pdf(object_key, expected_size=file_size)
    started = time.time()

    result = do_extract(
        local_path,
        shard_index=shard_index,
        page_start=page_start,
        page_end=page_end,
    )

    # ---- OCR 兜底：图片型 / 扫描件 PDF ----
    # 某页抽不到文字块、但有图片（PDF 是整页扫描），交给阿里云 OCR 识别后并回块列表。
    pages_with_text = {b.page for b in result.blocks}
    need_ocr = [p for p in range(page_start, page_end + 1) if p not in pages_with_text]
    if need_ocr:
        try:
            from app.core.config import get_settings
            from app.services.ocr import (
                blocks_from_ocr,
                get_ocr_client,
                ocr_image,
                render_page_png,
            )

            settings = get_settings()
            client = get_ocr_client(settings)
            ocr_hits = 0
            for pno in need_ocr:
                try:
                    png = render_page_png(local_path, pno, settings.ocr_dpi)
                    text = ocr_image(client, png, settings.ocr_type)
                except Exception as exc:  # noqa: BLE001 - 单页失败不中断整本
                    logger.warning("OCR 第 %s 页失败，跳过：%s", pno, exc)
                    continue
                if text:
                    result.blocks.extend(blocks_from_ocr(text, pno))
                    ocr_hits += 1
            if ocr_hits:
                logger.info("分片 %s OCR 兜底识别了 %s 页", shard_index, ocr_hits)
        except Exception as exc:  # noqa: BLE001 - 客户端构建失败等整体异常
            logger.warning(
                "OCR 客户端不可用（可能未开通阿里云文字识别服务或缺少密钥）：%s", exc
            )

    elapsed = time.time() - started
    logger.info(
        "分片 %s 提取完成: P%s-%s -> %s 个块, 耗时 %.1fs",
        shard_index, page_start, page_end, len(result.blocks), elapsed,
    )

    if job_id:
        get_dm_store().bump_job(
            job_id,
            processed_pages=result.page_count,
            finished_shards=1,
            stage_detail=f"已完成分片 {shard_index}（P{page_start}-{page_end}）",
        )
    return result.to_dict()


# ============================================================
# T2：合并、分块、全局去重（单任务，需要全局视野）
# ============================================================
@_task(bind=True, name="dm.chunk_and_dedup", **_RETRY_OPTS)
def chunk_and_dedup(
    self,
    shard_payloads: Sequence[Dict[str, Any]],
    *,
    job_id: str,
    document_id: str,
    script_id: str,
    script_title: str = "",
    object_key: str = "",
) -> Dict[str, Any]:
    """chord 回调：把所有分片汇总成去重后的 chunk，并派发 T3→T4 流水线。

    这一步必须单任务串行 —— 页眉页脚识别要看全书的重复频次，
    标题层级校准要看全局字号分布，去重更是天然需要全局视野。
    好在它只做字符串运算，几十万字也就几秒钟。
    """
    store = get_dm_store()
    settings = get_settings()
    store.bump_job(job_id, status=JOB_CHUNKING, stage_detail="正在合并分片并语义分块")

    shards = [ShardResult.from_dict(p) for p in shard_payloads if p]
    if not shards:
        raise AppError("所有提取分片均无结果，PDF 可能是纯扫描件（需要 OCR）")

    blocks, total_pages = merge_shards(shards)
    blocks, dropped_noise = strip_noise(
        blocks, total_pages=total_pages, ratio_threshold=settings.dm_header_footer_ratio
    )
    calibrate_headings(blocks)
    section_paths = build_section_paths(blocks)

    # 语义分块器需要 embedding 才能算断点。没配 Key 就退回纯递归切分，
    # 分块质量略降但流水线照常跑通。
    embeddings = None
    if settings.siliconflow_api_key:
        from app.services.llm import SiliconFlowEmbeddings

        embeddings = SiliconFlowEmbeddings(get_llm_client())

    chunks = chunk_blocks(
        blocks,
        section_paths=section_paths,
        config=ChunkConfig.from_settings(settings),
        embeddings=embeddings,
    )

    # ---------- 全局去重 ----------
    backend = build_backend(
        settings.celery_redis_url,
        namespace=f"dm:dedup:{document_id}",
        ttl=7 * 24 * 3600,
    )
    dedup = Deduplicator(
        backend=backend,
        threshold=settings.dm_simhash_threshold,
        min_chars=settings.dm_min_chunk_chars,
    )

    kept: List[Dict[str, Any]] = []
    for chunk in chunks:
        verdict = dedup.check(chunk.text)
        if verdict.is_duplicate:
            continue
        payload = chunk.to_dict()
        payload["content_hash"] = verdict.content_hash
        payload["simhash"] = to_signed_64(verdict.fingerprint)
        kept.append(payload)

    # 去重后重新编号，保证 chunk_index 连续（前端按序展示时不会出现空洞）
    for i, payload in enumerate(kept):
        payload["chunk_index"] = i

    stats = dedup.stats.to_dict()
    logger.info(
        "分块去重完成 doc=%s: 块 %s -> %s (去重率 %.1f%%), 噪声行 %s",
        document_id, len(chunks), len(kept), stats["dedup_rate"] * 100, dropped_noise,
    )

    store.bump_job(
        job_id,
        status=JOB_GENERATING_QA,
        total_chunks=len(kept),
        dropped_chunks=dedup.stats.dropped + dropped_noise,
        stage_detail=f"得到 {len(kept)} 个有效块，开始生成问答对",
    )
    store.update_document(
        document_id,
        {
            "total_pages": total_pages,
            "total_chunks": len(kept),
            "dropped_chunks": dedup.stats.dropped + dropped_noise,
        },
    )

    if not kept:
        # 没有任何有效内容，直接收尾，不要留下一个永远 pending 的任务
        _mark_completed(store, job_id, document_id, chunk_count=0, qa_count=0)
        return {"chunks": 0, "batches": 0, "dedup": stats}

    batches = _split_batches(kept, max(1, settings.dm_qa_batch_size))

    if not celery_available():
        return {"chunks": len(kept), "batches": len(batches), "dedup": stats, "dispatched": False}

    from celery import chain, chord

    header = [
        chain(
            generate_qa.s(
                batch,
                job_id=job_id,
                document_id=document_id,
                script_id=script_id,
                script_title=script_title,
            ),
            embed_and_store.s(job_id=job_id, document_id=document_id, script_id=script_id),
        )
        for batch in batches
    ]
    callback = finalize.s(job_id=job_id, document_id=document_id, script_id=script_id)
    chord(header)(callback.on_error(on_pipeline_error.s(job_id=job_id)))

    return {
        "chunks": len(kept),
        "batches": len(batches),
        "dedup": stats,
        "dispatched": True,
    }


def _split_batches(items: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# ============================================================
# T3：问答对生成
# ============================================================
@_task(bind=True, name="dm.generate_qa", **_RETRY_OPTS)
def generate_qa(
    self,
    batch: Sequence[Dict[str, Any]],
    *,
    job_id: str,
    document_id: str,
    script_id: str,
    script_title: str = "",
) -> Dict[str, Any]:
    """给一批 chunk 生成问答对，把 chunk 原样透传给下游 T4。

    透传而非让 T4 重新查库，是为了让「生成 → 向量化」两步共享同一份内存数据，
    省掉一次往返。批次大小由 ``dm_qa_batch_size`` 控制，几个 chunk 的文本量
    走 broker 传递完全可以接受。
    """
    chunks = list(batch)
    if not chunks:
        return {"chunks": [], "qa": []}

    pairs: List[QAPair] = get_llm_client().generate_qa(chunks, script_title=script_title)

    if job_id and pairs:
        get_dm_store().bump_job(job_id, total_qa=len(pairs))

    return {
        "chunks": chunks,
        "qa": [p.to_dict() for p in pairs],
    }


# ============================================================
# T4：向量化并入库
# ============================================================
@_task(bind=True, name="dm.embed_and_store", **_RETRY_OPTS)
def embed_and_store(
    self,
    payload: Dict[str, Any],
    *,
    job_id: str,
    document_id: str,
    script_id: str,
) -> Dict[str, Any]:
    """把一批 chunk 与其问答对向量化后写入 Supabase。"""
    chunks: List[Dict[str, Any]] = list(payload.get("chunks") or [])
    qa_items: List[Dict[str, Any]] = list(payload.get("qa") or [])
    if not chunks:
        return {"chunks": 0, "qa": 0}

    store = get_dm_store()
    client = get_llm_client()

    # ---------- chunk 向量 ----------
    chunk_objs = chunks_from_payload(chunks)
    # 带章节面包屑去向量化，正文原样入库
    chunk_vectors = client.embed_documents([c.embedding_text() for c in chunk_objs])

    chunk_rows: List[Dict[str, Any]] = []
    for payload_item, chunk_obj, vector in zip(chunks, chunk_objs, chunk_vectors):
        chunk_rows.append(
            {
                "document_id": document_id,
                "script_id": script_id,
                "chunk_index": payload_item.get("chunk_index", chunk_obj.chunk_index),
                "content": chunk_obj.text,
                "content_hash": payload_item["content_hash"],
                "simhash": payload_item.get("simhash"),
                "page_start": chunk_obj.page_start,
                "page_end": chunk_obj.page_end,
                "section_path": chunk_obj.section_path,
                "block_type": chunk_obj.block_type,
                "char_count": chunk_obj.char_count,
                "embedding": to_pgvector(vector),
            }
        )

    inserted = store.insert_chunks(chunk_rows)
    # 按 content_hash 建映射而不是靠返回顺序：PostgREST 在 upsert 冲突时
    # 返回的行序不保证与请求一致，靠下标对齐会把 QA 挂到错误的 chunk 上
    hash_to_id = {row.get("content_hash"): row.get("id") for row in inserted}

    # ---------- QA 向量 ----------
    qa_rows: List[Dict[str, Any]] = []
    if qa_items:
        questions = [item["question"] for item in qa_items]
        qa_vectors = client.embed_documents(questions)

        for item, vector in zip(qa_items, qa_vectors):
            idx = int(item.get("source_index", 0))
            source = chunks[idx] if 0 <= idx < len(chunks) else chunks[0]
            chunk_id = hash_to_id.get(source.get("content_hash"))
            question = item["question"]
            qa_rows.append(
                {
                    "document_id": document_id,
                    "script_id": script_id,
                    "chunk_id": chunk_id,
                    "question": question,
                    "answer": item["answer"],
                    "question_hash": hashlib.sha256(
                        question.strip().encode("utf-8")
                    ).hexdigest(),
                    "category": item.get("category") or "general",
                    "page_start": source.get("page_start"),
                    "page_end": source.get("page_end"),
                    "section_path": source.get("section_path") or [],
                    "embedding": to_pgvector(vector),
                }
            )
        store.insert_qa(qa_rows)

    if job_id:
        store.bump_job(
            job_id,
            status=JOB_EMBEDDING,
            embedded_chunks=len(chunk_rows),
            embedded_qa=len(qa_rows),
            stage_detail=f"已向量化 {len(chunk_rows)} 块 / {len(qa_rows)} 问答",
        )

    logger.info(
        "入库完成 doc=%s: chunk=%s qa=%s", document_id, len(chunk_rows), len(qa_rows)
    )
    return {"chunks": len(chunk_rows), "qa": len(qa_rows)}


# ============================================================
# 收尾与错误处理
# ============================================================
def _mark_completed(
    store: "store_mod.DMStore",
    job_id: str,
    document_id: str,
    *,
    chunk_count: int,
    qa_count: int,
) -> None:
    store.update_document(
        document_id, {"total_chunks": chunk_count, "total_qa": qa_count}
    )
    store.update_job(
        job_id,
        {
            "status": JOB_COMPLETED,
            "stage_detail": f"完成：{chunk_count} 个块 / {qa_count} 条问答对",
            "finished_at": "now()",
            "error_message": None,
        },
    )


@_task(bind=True, name="dm.finalize")
def finalize(
    self,
    results: Sequence[Dict[str, Any]],
    *,
    job_id: str,
    document_id: str,
    script_id: str = "",
) -> Dict[str, Any]:
    """所有批次入库完毕后收尾。

    最终计数以**数据库实际行数**为准，而不是累加各批次的返回值 ——
    重试导致的重复执行会让累加值虚高，而唯一约束保证了库里的行数才是真相。
    """
    store = get_dm_store()
    chunk_count = store.count_chunks(document_id)
    qa_count = store.count_qa(document_id)

    _mark_completed(store, job_id, document_id, chunk_count=chunk_count, qa_count=qa_count)

    # 新版本入库成功后才让旧版本下线，避免中途失败时新旧都不可用
    if script_id:
        store.deactivate_other_versions(script_id, document_id)

    batches = len(results) if results else 0
    logger.info(
        "流水线完成 doc=%s: %s 批次 -> %s 块 / %s 问答",
        document_id, batches, chunk_count, qa_count,
    )
    return {"document_id": document_id, "chunks": chunk_count, "qa": qa_count}


@_task(bind=True, name="dm.on_pipeline_error")
def on_pipeline_error(self, request: Any, exc: Any = None, traceback: Any = None, *, job_id: str = "") -> None:
    """link_error 回调：把失败信息落到任务记录上。

    Celery 的错误回调签名是 (request, exc, traceback)，与普通任务不同。
    这里不重新抛出异常，否则错误处理器自身失败会淹没真正的错误原因。
    """
    message = f"{type(exc).__name__ if exc else 'Error'}: {exc}"
    logger.error("DM 流水线失败 job=%s: %s", job_id, message)
    if not job_id:
        return
    try:
        get_dm_store().fail_job(job_id, message)
    except Exception as inner:  # noqa: BLE001
        logger.error("回写失败状态时再次出错 job=%s: %s", job_id, inner)


# ============================================================
# 对外派发入口
# ============================================================
def dispatch_pipeline(
    *,
    job_id: str,
    script_id: str,
    object_key: str,
    file_name: str = "",
    file_id: str = "",
    file_size: int = 0,
    script_title: str = "",
    force: bool = False,
) -> Optional[str]:
    """派发整条流水线，返回 Celery 任务 id（未启用 Celery 时返回 None）。"""
    if not celery_available():
        logger.warning("Celery 未安装或未配置，跳过 DM 流水线派发")
        return None

    async_result = prepare_document.apply_async(
        args=[job_id, script_id, object_key],
        kwargs={
            "file_name": file_name,
            "file_id": file_id,
            "file_size": file_size,
            "script_title": script_title,
            "force": force,
        },
        link_error=on_pipeline_error.s(job_id=job_id),
    )
    logger.info("DM 流水线已派发 job=%s task=%s", job_id, async_result.id)
    return async_result.id
