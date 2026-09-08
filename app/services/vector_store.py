"""本地 pgvector 向量库访问层。

Supabase 在境外，一次检索往返约 200ms，其中网络占 90%。向量表下沉到
同机 pgvector 后走 unix/docker 网络直连，检索降到 3~5ms。

设计要点：
  1. **与 DMStore 同签名**：``match_chunks`` / ``match_qa`` / ``insert_chunks``
     等方法的入参出参与 Supabase 侧完全一致，由 ``VECTOR_BACKEND`` 开关切换，
     上层无感；
  2. **embedding 一律用 pgvector 字面量**：``to_pgvector`` 输出 ``[0.1,0.2]``
     字符串，SQL 里 ``%s::vector`` 转换。psycopg 不需要注册 vector 类型，
     也就不用依赖 pgvector 的 Python 包版本；
  3. **批量写用多行 VALUES + ON CONFLICT DO UPDATE**，一次往返写一批。
     psycopg3 的 executemany 拿不到 RETURNING，所以手工拼多行占位符；
  4. **写入 id 可为空**：``coalesce(%s::uuid, gen_random_uuid())``，
     正常入库走自增，数据迁移时可以把 Supabase 的原 id 原样搬过来，
     保证 QA 的 chunk_id 外键不断。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Sequence

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.exceptions import DatabaseError

logger = logging.getLogger(__name__)


def to_pgvector(vector: Sequence[float]) -> str:
    """把 Python 浮点列表转成 pgvector 字面量 ``[0.1,0.2,...]``。

    经 JSON 传 vector 类型会被当成 text 解析失败，必须自己拼字符串。
    """
    return "[" + ",".join(f"{float(v):.7g}" for v in vector) + "]"


# 与 sql/local_vector.sql 的表结构一一对应。
# 写入时按这个顺序取 row 的字段，缺失的补 None。
CHUNK_COLUMNS = (
    "id",
    "document_id",
    "script_id",
    "script_code",
    "chunk_index",
    "content",
    "content_hash",
    "simhash",
    "page_start",
    "page_end",
    "section_path",
    "block_type",
    "char_count",
    "embedding",
)

QA_COLUMNS = (
    "id",
    "document_id",
    "script_id",
    "script_code",
    "chunk_id",
    "question",
    "answer",
    "question_hash",
    "category",
    "title",
    "page_start",
    "page_end",
    "section_path",
    "created_by",
    "embedding",
)

# 需要额外 cast 的列：vector 列必须显式转类型，id 允许为空（迁移时带原 id）
_CAST: Dict[str, str] = {
    "embedding": "%s::vector",
    "id": "coalesce(%s::uuid, gen_random_uuid())",
}

# 冲突时刷新的列（不含主键与冲突键本身）
_CHUNK_UPDATES = (
    "chunk_index",
    "content",
    "simhash",
    "page_start",
    "page_end",
    "section_path",
    "block_type",
    "char_count",
    "embedding",
    "script_id",
    "script_code",
)

_QA_UPDATES = (
    "chunk_id",
    "question",
    "answer",
    "category",
    "title",
    "page_start",
    "page_end",
    "section_path",
    "created_by",
    "embedding",
    "script_id",
    "script_code",
)

# 单行向量字面量约 9.5KB，100 行一批 ≈ 1MB/语句，兼顾语句长度与往返次数
_BATCH_SIZE = 100


class LocalVectorStore:
    """本地 pgvector 的同步访问器（与 DMStore 一样跑在线程池里）。"""

    def __init__(self, dsn: str, pool_max: int = 8) -> None:
        if not dsn:
            raise DatabaseError("本地向量库未配置 VECTOR_DB_DSN")
        self._dsn = dsn
        self._pool_max = max(1, int(pool_max))
        self._pool: Optional[ConnectionPool] = None
        self._lock = threading.Lock()

    # ---------------- 底层 ----------------
    @property
    def pool(self) -> ConnectionPool:
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    self._pool = ConnectionPool(
                        conninfo=self._dsn,
                        min_size=1,
                        max_size=self._pool_max,
                        timeout=30.0,
                        # 建连即注册 dict_row，省得每个 cursor 再传一次
                        kwargs={"row_factory": dict_row, "autocommit": True},
                    )
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def health(self) -> bool:
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("select 1")
                    cur.fetchone()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("本地向量库健康检查失败: %s", exc)
            return False

    def _fetch(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    return list(cur.fetchall())
        except Exception as exc:  # noqa: BLE001
            logger.error("本地向量库查询失败: %s | sql=%s", exc, sql[:200])
            raise DatabaseError(f"本地向量库查询失败: {exc}") from exc

    def _execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> None:
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
        except Exception as exc:  # noqa: BLE001
            logger.error("本地向量库写入失败: %s | sql=%s", exc, sql[:200])
            raise DatabaseError(f"本地向量库写入失败: {exc}") from exc

    def _bulk_upsert(
        self,
        table: str,
        columns: Sequence[str],
        rows: List[Dict[str, Any]],
        conflict_target: str,
        update_columns: Sequence[str],
        returning: str,
        batch_size: int = _BATCH_SIZE,
    ) -> List[Dict[str, Any]]:
        """多行 VALUES + ON CONFLICT DO UPDATE，一批一次往返。"""
        if not rows:
            return []

        col_sql = ", ".join(columns)
        row_ph = "(" + ", ".join(_CAST.get(c, "%s") for c in columns) + ")"
        upd_sql = ", ".join(f"{c} = excluded.{c}" for c in update_columns)

        out: List[Dict[str, Any]] = []
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            values_sql = ", ".join([row_ph] * len(batch))
            stmt = (
                f"insert into {table} ({col_sql}) values {values_sql} "
                f"on conflict ({conflict_target}) do update set {upd_sql} "
                f"returning {returning}"
            )
            params: List[Any] = [row.get(c) for row in batch for c in columns]
            out.extend(self._fetch(stmt, params))
        return out

    # ---------------- 分块 ----------------
    def insert_chunks(self, rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not rows:
            return []
        return self._bulk_upsert(
            "public.script_dm_chunks",
            CHUNK_COLUMNS,
            list(rows),
            "document_id, content_hash",
            _CHUNK_UPDATES,
            "id, content_hash",
        )

    def insert_qa(self, rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not rows:
            return []
        # 同批次内重复的 question_hash 会让 ON CONFLICT DO UPDATE 报
        # 「cannot affect row a second time」，入库前先按 hash 去重（保留第一条）
        seen: set = set()
        uniq: List[Dict[str, Any]] = []
        for r in rows:
            key = r.get("question_hash")
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        if len(uniq) != len(rows):
            logger.info("insert_qa 同批次去重：%s -> %s 条", len(rows), len(uniq))

        return self._bulk_upsert(
            "public.script_dm_qa",
            QA_COLUMNS,
            uniq,
            "document_id, question_hash",
            _QA_UPDATES,
            "id, question_hash",
        )

    def count_chunks(self, document_id: str) -> int:
        rows = self._fetch(
            "select count(*)::int as n from public.script_dm_chunks where document_id = %s::uuid",
            [document_id],
        )
        return int(rows[0]["n"]) if rows else 0

    def count_qa(self, document_id: str) -> int:
        rows = self._fetch(
            "select count(*)::int as n from public.script_dm_qa where document_id = %s::uuid",
            [document_id],
        )
        return int(rows[0]["n"]) if rows else 0

    # ---------------- 检索 ----------------
    def match_chunks(
        self,
        embedding: Sequence[float],
        *,
        script_id: Optional[str] = None,
        script_code: Optional[str] = None,
        document_id: Optional[str] = None,
        match_count: int = 8,
        similarity_threshold: float = 0.25,
    ) -> List[Dict[str, Any]]:
        return self._fetch(
            "select * from public.match_dm_chunks("
            " %s::vector, %s::uuid, %s::uuid, %s, %s, %s)",
            [
                to_pgvector(embedding),
                script_id,
                document_id,
                script_code,
                match_count,
                similarity_threshold,
            ],
        )

    def match_qa(
        self,
        embedding: Sequence[float],
        *,
        script_id: Optional[str] = None,
        script_code: Optional[str] = None,
        document_id: Optional[str] = None,
        category: Optional[str] = None,
        match_count: int = 8,
        similarity_threshold: float = 0.25,
    ) -> List[Dict[str, Any]]:
        return self._fetch(
            "select * from public.match_dm_qa("
            " %s::vector, %s::uuid, %s::uuid, %s, %s, %s, %s)",
            [
                to_pgvector(embedding),
                script_id,
                document_id,
                script_code,
                category,
                match_count,
                similarity_threshold,
            ],
        )

    def list_qa_titles(self, script_code: str) -> List[Dict[str, Any]]:
        return self._fetch("select * from public.list_dm_qa_titles(%s)", [script_code])

    # ---------------- 文档影子表同步 ----------------
    def sync_document(self, doc: Dict[str, Any]) -> None:
        """同步 Supabase 主表的一行到本地影子表（检索过滤要用）。"""
        if not doc.get("id"):
            return
        self._execute(
            "insert into public.dm_documents"
            " (id, script_id, script_code, object_key, is_active, deleted_at, created_at)"
            " values (%s::uuid, %s::uuid, %s, %s, %s, %s, %s)"
            " on conflict (id) do update set"
            "  script_id = excluded.script_id,"
            "  script_code = excluded.script_code,"
            "  object_key = excluded.object_key,"
            "  is_active = excluded.is_active,"
            "  deleted_at = excluded.deleted_at,"
            "  created_at = coalesce(excluded.created_at, public.dm_documents.created_at)",
            [
                doc.get("id"),
                doc.get("script_id"),
                doc.get("script_code") or "",
                doc.get("object_key") or "",
                bool(doc.get("is_active", True)),
                doc.get("deleted_at"),
                doc.get("created_at"),
            ],
        )

    def deactivate_other_versions(self, script_id: str, keep_document_id: str) -> None:
        """与 DMStore.deactivate_other_versions 同语义：同剧本的旧版本下线。"""
        self._execute(
            "update public.dm_documents set is_active = false"
            " where script_id = %s::uuid and id <> %s::uuid and is_active",
            [script_id, keep_document_id],
        )

    def deactivate_documents_not_matching(
        self, script_id: str, object_key: str, *, script_code: Optional[str] = None
    ) -> None:
        """换文件重传：把不是当前 object_key 的激活文档全部下线。"""
        if script_code:
            self._execute(
                "update public.dm_documents set is_active = false"
                " where script_id = %s::uuid and object_key <> %s and is_active"
                "   and script_code = %s and deleted_at is null",
                [script_id, object_key, script_code],
            )
        else:
            self._execute(
                "update public.dm_documents set is_active = false"
                " where script_id = %s::uuid and object_key <> %s and is_active"
                "   and deleted_at is null",
                [script_id, object_key],
            )

    # ---------------- 清理 ----------------
    def purge_document(self, document_id: str) -> None:
        self._execute("select public.purge_local_document(%s::uuid)", [document_id])

    def purge_script(self, script_id: str) -> None:
        self._execute("select public.purge_local_script(%s::uuid)", [script_id])


# ---------------- 单例 ----------------
_store: Optional[LocalVectorStore] = None
_store_lock = threading.Lock()


def get_vector_store(settings: Any = None) -> LocalVectorStore:
    """按配置创建/复用本地向量库实例。未启用 local 后端时抛错。"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                if settings is None:
                    from app.core.config import get_settings

                    settings = get_settings()
                _store = LocalVectorStore(
                    dsn=settings.vector_db_dsn, pool_max=settings.vector_db_pool_max
                )
    return _store


def reset_vector_store() -> None:
    """测试与配置热更新用：丢弃当前实例。"""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None
