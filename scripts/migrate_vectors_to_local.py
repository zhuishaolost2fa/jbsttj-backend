"""把 Supabase 的存量向量数据搬到同机 pgvector。

用法（在服务器上，能连到 pgvector 容器的环境）：

    # 先干跑，看能搬多少、两边差多少
    python scripts/migrate_vectors_to_local.py --dry-run

    # 正式搬（可重复执行，按主键跳过已存在的行）
    python scripts/migrate_vectors_to_local.py

    # 容器内执行（推荐，网络与配置都现成）：
    docker compose -f docker-compose.prod.yml exec -T api \
        python scripts/migrate_vectors_to_local.py

搬什么：
  - script_dm_chunks（正文向量）
  - script_dm_qa（问题向量，引用 chunk_id，必须在 chunks 之后搬）
  - dm_documents 影子表（检索过滤用的 is_active / deleted_at / created_at）

不搬：scripts / documents / jobs / stories / highlights / questions 仍在 Supabase。

注意：
  1. 幂等 —— 用 on conflict (id) do nothing，中断后重跑会跳过已搬的行；
  2. 保留原 id —— QA 的 chunk_id 外键、stories 的 chunk_id 引用都依赖它；
  3. 只搬不删 —— Supabase 侧的存量数据保留，确认新链路稳定后再手工清理。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any, Dict, List

import httpx
import psycopg
from psycopg.rows import dict_row

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_vectors")

PAGE_SIZE = 1000

CHUNK_INSERT = """
insert into public.script_dm_chunks
    (id, document_id, script_id, script_code, chunk_index, content, content_hash,
     simhash, page_start, page_end, section_path, block_type, char_count,
     embedding, created_at)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
on conflict (id) do nothing
"""

QA_INSERT = """
insert into public.script_dm_qa
    (id, document_id, script_id, script_code, chunk_id, question, answer,
     question_hash, category, title, page_start, page_end, section_path,
     created_by, embedding, created_at)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
on conflict (id) do nothing
"""

DOC_INSERT = """
insert into public.dm_documents
    (id, script_id, script_code, object_key, is_active, deleted_at, created_at)
values (%s, %s, %s, %s, %s, %s, %s)
on conflict (id) do update set
    script_id = excluded.script_id,
    script_code = excluded.script_code,
    object_key = excluded.object_key,
    is_active = excluded.is_active,
    deleted_at = excluded.deleted_at,
    created_at = excluded.created_at
"""


def _fetch_all(
    client: httpx.Client,
    base: str,
    headers: Dict[str, str],
    table: str,
    select: str,
    limit: int,
) -> List[Dict[str, Any]]:
    """按 id 升序分页拉全表。

    PostgREST 的 count 走 Content-Range，这里直接靠「本页是否满」判断结束，
    不依赖 total 字段。
    """
    out: List[Dict[str, Any]] = []
    offset = 0
    while True:
        if limit and len(out) >= limit:
            break
        resp = client.get(
            f"{base}/rest/v1/{table}",
            params={
                "select": select,
                "order": "id",
                "offset": offset,
                "limit": PAGE_SIZE,
            },
            headers={**headers, "Range-Unit": "items", "Prefer": "count=exact"},
            timeout=120.0,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"读取 {table} 失败 {resp.status_code}: {resp.text[:300]}")
        rows = resp.json()
        out.extend(rows)
        cr = resp.headers.get("content-range", "")
        total = int(cr.split("/")[1]) if "/" in cr and cr.split("/")[1] else None
        logger.info("  %s: 已读 %s 行%s", table, len(out), f" / {total}" if total else "")
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移 Supabase 向量数据到本地 pgvector")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--limit", type=int, default=0, help="每张表最多搬多少行（调试用）")
    parser.add_argument("--dsn", default=os.getenv("VECTOR_DB_DSN", ""), help="本地库 DSN")
    args = parser.parse_args()

    dsn = args.dsn
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not dsn or not supabase_url or not service_key:
        logger.error("缺少 VECTOR_DB_DSN / SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        return 2

    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Accept": "application/json",
    }

    started = time.perf_counter()
    with httpx.Client(timeout=120.0, headers=headers) as client:
        logger.info("读取 Supabase 存量数据…")
        chunks = _fetch_all(
            client,
            supabase_url,
            headers,
            "script_dm_chunks",
            "id,document_id,script_id,script_code,chunk_index,content,content_hash,"
            "simhash,page_start,page_end,section_path,block_type,char_count,embedding,created_at",
            args.limit,
        )
        qa = _fetch_all(
            client,
            supabase_url,
            headers,
            "script_dm_qa",
            "id,document_id,script_id,script_code,chunk_id,question,answer,question_hash,"
            "category,title,page_start,page_end,section_path,created_by,embedding,created_at",
            args.limit,
        )
        docs = _fetch_all(
            client,
            supabase_url,
            headers,
            "script_dm_documents",
            "id,script_id,script_code,object_key,is_active,deleted_at,created_at",
            args.limit,
        )

    logger.info(
        "Supabase 侧：chunks=%s qa=%s documents=%s（耗时 %.1fs）",
        len(chunks),
        len(qa),
        len(docs),
        time.perf_counter() - started,
    )

    if args.dry_run:
        logger.info("--dry-run：不写入本地库，退出")
        return 0

    if not dsn:
        logger.error("缺少 VECTOR_DB_DSN")
        return 2

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # 顺序不能反：qa.chunk_id 引用 chunks
            logger.info("写入 dm_documents 影子表…")
            cur.executemany(
                DOC_INSERT,
                [
                    (
                        d.get("id"),
                        d.get("script_id"),
                        d.get("script_code") or "",
                        d.get("object_key") or "",
                        bool(d.get("is_active", True)),
                        d.get("deleted_at"),
                        d.get("created_at"),
                    )
                    for d in docs
                ],
            )

            logger.info("写入 script_dm_chunks…")
            cur.executemany(
                CHUNK_INSERT,
                [
                    (
                        c.get("id"),
                        c.get("document_id"),
                        c.get("script_id"),
                        c.get("script_code") or "",
                        c.get("chunk_index"),
                        c.get("content"),
                        c.get("content_hash"),
                        c.get("simhash"),
                        c.get("page_start"),
                        c.get("page_end"),
                        c.get("section_path") or [],
                        c.get("block_type") or "body",
                        c.get("char_count") or 0,
                        c.get("embedding"),
                        c.get("created_at"),
                    )
                    for c in chunks
                ],
            )

            logger.info("写入 script_dm_qa…")
            cur.executemany(
                QA_INSERT,
                [
                    (
                        q.get("id"),
                        q.get("document_id"),
                        q.get("script_id"),
                        q.get("script_code") or "",
                        q.get("chunk_id"),
                        q.get("question"),
                        q.get("answer"),
                        q.get("question_hash"),
                        q.get("category") or "other",
                        q.get("title") or "",
                        q.get("page_start"),
                        q.get("page_end"),
                        q.get("section_path") or [],
                        q.get("created_by"),
                        q.get("embedding"),
                        q.get("created_at"),
                    )
                    for q in qa
                ],
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("select count(*)::int as n from public.script_dm_chunks")
            local_chunks = cur.fetchone()["n"]
            cur.execute("select count(*)::int as n from public.script_dm_qa")
            local_qa = cur.fetchone()["n"]
            cur.execute("select count(*)::int as n from public.dm_documents")
            local_docs = cur.fetchone()["n"]
            # 统计信息更新后 HNSW 规划器才能选对索引
            cur.execute("analyze public.script_dm_chunks")
            cur.execute("analyze public.script_dm_qa")

    logger.info(
        "完成：本地 chunks=%s（源 %s） qa=%s（源 %s） documents=%s（源 %s），总耗时 %.1fs",
        local_chunks,
        len(chunks),
        local_qa,
        len(qa),
        local_docs,
        len(docs),
        time.perf_counter() - started,
    )
    if local_chunks < len(chunks) or local_qa < len(qa):
        logger.warning("本地行数少于源端，可能有行被 on conflict 跳过（正常：重跑时）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
