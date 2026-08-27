"""DM 指南 RAG 的数据访问层（**同步版**）。

项目里已经有一个异步的 :class:`~app.services.supabase.SupabaseClient`，
为什么还要再写一个同步的？

因为 Celery 用 prefork 多进程模型，worker 主循环是同步的。在里面
`asyncio.run(...)` 每个任务起一个 event loop，会带来几个实打实的坑：

  - 与 Celery 的 SIGTERM 优雅退出打架，worker 关不干净；
  - httpx.AsyncClient 的连接池绑定在 loop 上，loop 一关连接全废，
    每个任务都要重建连接，长任务里这是不小的开销；
  - billiard fork 出来的子进程继承了父进程的 loop 状态，行为难以预测。

所以 worker 侧走这一份 `httpx.Client` 同步实现，FastAPI 侧继续用异步那份，
两边各自访问最顺手的传输层。表结构定义见 ``sql/dm_rag.sql``。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigError, DatabaseError

logger = logging.getLogger("app.dm_store")

TABLE_DOCUMENTS = "script_dm_documents"
TABLE_CHUNKS = "script_dm_chunks"
TABLE_QA = "script_dm_qa"
TABLE_JOBS = "script_dm_jobs"
TABLE_QUESTIONS = "script_dm_questions"
TABLE_STORIES = "script_dm_stories"
TABLE_HIGHLIGHTS = "script_dm_highlights"

# 故事还原 / 划线评论的状态与可见性枚举（与 sql/dm_story.sql 保持一致）
STORY_TYPES = ("timeline", "truth", "role", "clue", "ending", "other")
HL_VISIBILITY = frozenset({"private", "public"})
HL_STATUS_ACTIVE = "active"
HL_STATUS_ORPHANED = "orphaned"

# 问答标题链（qa-titles 接口）Redis 缓存的 scope 前缀。
# API 侧读缓存、ingest 流水线写库后失效缓存都从这一处取，保证两端口径一致。
QA_TITLES_CACHE_SCOPE = "dm-qa-titles"


def qa_titles_cache_scope(script_code: str) -> str:
    """某个剧本的问答标题链缓存 scope（script_code 统一小写后拼接）。"""
    return f"{QA_TITLES_CACHE_SCOPE}:{(script_code or '').strip().lower()}"

# 用户提问状态机：pending 待解答 → answered 已解答 / dismissed 无效问题
QUESTION_PENDING = "pending"
QUESTION_ANSWERED = "answered"
QUESTION_DISMISSED = "dismissed"
QUESTION_STATES = frozenset({QUESTION_PENDING, QUESTION_ANSWERED, QUESTION_DISMISSED})

# 任务状态机：pending → downloading → extracting → chunking → generating_qa
#            → embedding → completed / failed / cancelled
JOB_PENDING = "pending"
JOB_DOWNLOADING = "downloading"
JOB_EXTRACTING = "extracting"
JOB_CHUNKING = "chunking"
JOB_GENERATING_QA = "generating_qa"
JOB_EMBEDDING = "embedding"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
# 内容指纹命中已完成的旧版本，整条流水线跳过 —— 与 completed 区分开，
# 便于统计「真正跑了多少次解析」和「省下了多少次」
JOB_SKIPPED = "skipped"

TERMINAL_STATES = frozenset({JOB_COMPLETED, JOB_FAILED, JOB_CANCELLED, JOB_SKIPPED})

# 与 sql/dm_rag.sql 的 ck_dm_job_status 保持一致。
# 这里冗余一份是为了能在应用侧提前拦截，而不是等 PG 抛约束错误 ——
# 后者的报错信息里只有约束名，排查时得去翻 SQL 才知道哪个值不合法。
JOB_STATES = frozenset(
    {
        JOB_PENDING,
        JOB_DOWNLOADING,
        JOB_EXTRACTING,
        JOB_CHUNKING,
        JOB_GENERATING_QA,
        JOB_EMBEDDING,
        JOB_COMPLETED,
        JOB_FAILED,
        JOB_CANCELLED,
        JOB_SKIPPED,
    }
)


def to_pgvector(vector: Sequence[float]) -> str:
    """把 Python 浮点列表转成 pgvector 的字面量 ``[0.1,0.2,...]``。

    PostgREST 走 JSON，vector 类型收到 JSON 数组时会当成 text 解析失败，
    必须自己拼成字符串再交给 PG 隐式转换。
    """
    return "[" + ",".join(f"{float(v):.7g}" for v in vector) + "]"


class DMStore:
    """DM RAG 相关表的同步访问器。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._client: Optional[httpx.Client] = None

    # ---------------- 底层 ----------------
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            s = self._settings
            if not s.supabase_url or not s.supabase_service_role_key:
                raise ConfigError("Supabase 未配置，无法写入 DM 向量库")
            self._client = httpx.Client(
                base_url=s.supabase_rest_url,
                headers={
                    "apikey": s.supabase_service_role_key,
                    "Authorization": f"Bearer {s.supabase_service_role_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                # 批量写入带 1024 维向量的行，包体可达数 MB，超时给宽松些
                timeout=httpx.Timeout(120.0, connect=10.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            resp = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            logger.error("DM 库请求失败 %s %s: %s", method, path, exc)
            raise DatabaseError(f"数据库请求失败: {exc}") from exc

        if resp.status_code >= 400:
            detail: Any
            try:
                detail = resp.json()
            except Exception:  # noqa: BLE001
                detail = resp.text[:500]
            logger.error("DM 库 %s %s -> %s %s", method, path, resp.status_code, detail)
            raise DatabaseError("数据库操作失败", details=detail)
        return resp

    @staticmethod
    def _rows(resp: httpx.Response) -> List[Dict[str, Any]]:
        if resp.status_code == 204 or not resp.content:
            return []
        data = resp.json()
        return data if isinstance(data, list) else [data]

    def rpc(self, name: str, params: Dict[str, Any]) -> Any:
        resp = self._request("POST", f"/rpc/{name}", json=params)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ---------------- 文档 ----------------
    def upsert_document(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """按 (script_id, content_hash) 幂等写入文档记录。

        用文件内容指纹而非 object_key 做幂等键：同一份 PDF 被重复上传成
        两个 object_key 是常事（前端重试、用户手滑），按内容去重才能真正
        避免同一本手册被向量化两遍。
        """
        resp = self._request(
            "POST",
            f"/{TABLE_DOCUMENTS}",
            params={"on_conflict": "script_id,content_hash"},
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            json=[payload],
        )
        rows = self._rows(resp)
        if not rows:
            raise DatabaseError("文档记录写入后未返回数据")
        return rows[0]

    def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        resp = self._request(
            "GET", f"/{TABLE_DOCUMENTS}", params={"id": f"eq.{document_id}", "select": "*", "limit": 1}
        )
        rows = self._rows(resp)
        return rows[0] if rows else None

    def get_active_document(
        self, script_id: str = "", *, script_code: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """取当前参与检索的手册。

        传 `script_code` 时按业务 code 查询，用于同名剧本被拆成多个 script_id 后的
        详情页聚合；否则保持旧行为，只取单个 script_id 下的最新活跃版本。
        如果新字段暂时还没回填，自动退回到 script_id 查询，保证旧库可用。
        """
        params: Dict[str, Any] = {
            "is_active": "eq.true",
            "deleted_at": "is.null",
            "select": "*",
            "order": "version.desc",
            "limit": 1,
        }
        if script_code:
            params["script_code"] = f"eq.{script_code}"
        else:
            params["script_id"] = f"eq.{script_id}"
        resp = self._request("GET", f"/{TABLE_DOCUMENTS}", params=params)
        rows = self._rows(resp)
        if rows or not script_code or not script_id or script_code == script_id:
            return rows[0] if rows else None
        legacy_params = dict(params)
        legacy_params.pop("script_code", None)
        legacy_params["script_id"] = f"eq.{script_id}"
        resp = self._request("GET", f"/{TABLE_DOCUMENTS}", params=legacy_params)
        rows = self._rows(resp)
        return rows[0] if rows else None

    def list_active_documents_by_code(self, script_code: str) -> List[Dict[str, Any]]:
        """取同一业务 code 下所有活跃手册，用于详情页聚合状态。"""
        resp = self._request(
            "GET",
            f"/{TABLE_DOCUMENTS}",
            params={
                "script_code": f"eq.{script_code}",
                "is_active": "eq.true",
                "deleted_at": "is.null",
                "select": "*",
                "order": "created_at.desc",
            },
        )
        return self._rows(resp)

    def latest_job(
        self,
        script_id: str = "",
        *,
        script_code: Optional[str] = None,
        object_key: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """取最近一次任务，不论是否已结束。

        与 :meth:`find_active_job` 的区别：那个只看在跑的，用于防重复派发；
        这个包含终态，用于在详情页展示「上次解析失败了，原因是 XXX」。

        `object_key` 传入时，只在该文件对应的任务里取最新一条 —— 用于在
        `extra.dmGuide` 已经换成新文件、但库里还残留着旧文件任务时，
        把「解析状态」正确锚定到当前关联的文件，而不是被旧文件的任务带偏。
        """
        params: Dict[str, Any] = {
            "select": "*",
            "order": "created_at.desc",
            "limit": 1,
        }
        if script_code:
            params["script_code"] = f"eq.{script_code}"
        else:
            params["script_id"] = f"eq.{script_id}"
        if object_key:
            params["object_key"] = f"eq.{object_key}"
        resp = self._request("GET", f"/{TABLE_JOBS}", params=params)
        rows = self._rows(resp)
        if rows or not script_code or not script_id or script_code == script_id:
            return rows[0] if rows else None
        legacy_params = dict(params)
        legacy_params.pop("script_code", None)
        legacy_params["script_id"] = f"eq.{script_id}"
        resp = self._request("GET", f"/{TABLE_JOBS}", params=legacy_params)
        rows = self._rows(resp)
        return rows[0] if rows else None

    def update_document(self, document_id: str, patch: Dict[str, Any]) -> None:
        self._request(
            "PATCH",
            f"/{TABLE_DOCUMENTS}",
            params={"id": f"eq.{document_id}"},
            headers={"Prefer": "return=minimal"},
            json=patch,
        )

    def deactivate_other_versions(
        self, script_id: str, keep_document_id: str, *, script_code: Optional[str] = None
    ) -> None:
        """把同一剧本实例下的旧版本文档置为非激活。

        注意这里只按 `script_id` 下线旧版本，不按 `script_code` 下线。因为同一业务 code
        可能对应多个分片剧本，各片段的手册都应保持 active，供详情页聚合检索。
        """
        self._request(
            "PATCH",
            f"/{TABLE_DOCUMENTS}",
            params={
                "script_id": f"eq.{script_id}",
                "id": f"neq.{keep_document_id}",
                "is_active": "eq.true",
            },
            headers={"Prefer": "return=minimal"},
            json={"is_active": False},
        )

    def deactivate_documents_not_matching(
        self, script_id: str, object_key: str, *, script_code: Optional[str] = None
    ) -> None:
        """把同一剧本下、不属于「当前文件」的激活文档全部下线。

        用于「换了文件重新上传」的场景：用户在旧文件解析还在跑的时候换了一份新文件，
        旧文件的文档仍是 `is_active=true`。若不处理，`get_status` 会按 `created_at`
        把旧文件的文档当成「最新」展示，解析状态的文件名就卡在旧文件上。
        这里只保留与当前 `object_key` 一致的文档为激活态。
        """
        params: Dict[str, Any] = {
            "script_id": f"eq.{script_id}",
            "object_key": f"neq.{object_key}",
            "is_active": "eq.true",
            "deleted_at": "is.null",
        }
        if script_code:
            params["script_code"] = f"eq.{script_code}"
        self._request(
            "PATCH",
            f"/{TABLE_DOCUMENTS}",
            params=params,
            headers={"Prefer": "return=minimal"},
            json={"is_active": False},
        )

    def next_version(self, script_id: str) -> int:
        resp = self._request(
            "GET",
            f"/{TABLE_DOCUMENTS}",
            params={
                "script_id": f"eq.{script_id}",
                "select": "version",
                "order": "version.desc",
                "limit": 1,
            },
        )
        rows = self._rows(resp)
        return int(rows[0].get("version", 0)) + 1 if rows else 1

    def purge_document(self, document_id: str) -> None:
        """清空某文档已入库的 chunk 与 QA（重跑前调用）。"""
        self.rpc("purge_dm_document", {"p_document_id": document_id})

    # ---------------- 任务 ----------------
    def create_job(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._request(
            "POST",
            f"/{TABLE_JOBS}",
            headers={"Prefer": "return=representation"},
            json=[payload],
        )
        rows = self._rows(resp)
        if not rows:
            raise DatabaseError("任务记录写入后未返回数据")
        return rows[0]

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        resp = self._request(
            "GET", f"/{TABLE_JOBS}", params={"id": f"eq.{job_id}", "select": "*", "limit": 1}
        )
        rows = self._rows(resp)
        return rows[0] if rows else None

    def find_active_job(self, script_id: str = "", *, script_code: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """查询是否已有在跑的任务，避免重复派发整条流水线。"""
        params: Dict[str, Any] = {
            "status": f"not.in.({','.join(sorted(TERMINAL_STATES))})",
            "select": "*",
            "order": "created_at.desc",
            "limit": 1,
        }
        if script_code:
            params["script_code"] = f"eq.{script_code}"
        else:
            params["script_id"] = f"eq.{script_id}"
        resp = self._request("GET", f"/{TABLE_JOBS}", params=params)
        rows = self._rows(resp)
        return rows[0] if rows else None

    def update_job(self, job_id: str, patch: Dict[str, Any]) -> None:
        """整体覆盖式更新。**只用于单写者场景**（如 T1 起始、最终收尾）。

        并行阶段的计数器累加一律走 :meth:`bump_job`，否则会丢更新。
        """
        self._request(
            "PATCH",
            f"/{TABLE_JOBS}",
            params={"id": f"eq.{job_id}"},
            headers={"Prefer": "return=minimal"},
            json=patch,
        )

    def bump_job(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        stage_detail: Optional[str] = None,
        processed_pages: int = 0,
        finished_shards: int = 0,
        total_chunks: int = 0,
        dropped_chunks: int = 0,
        total_qa: int = 0,
        embedded_chunks: int = 0,
        embedded_qa: int = 0,
        total_stories: int = 0,
        embedded_stories: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """原子自增任务进度。

        并行的 T1 分片 / T4 批次会同时上报进度。若用「读出来 +1 再写回」，
        两个 worker 读到同一个旧值就会丢一次计数，进度条永远停在 90%。
        这里下推到 SQL 层做 ``field = field + delta``，天然免疫并发。
        """
        params: Dict[str, Any] = {
            "p_job_id": job_id,
            "p_processed_pages": processed_pages,
            "p_finished_shards": finished_shards,
            "p_total_chunks": total_chunks,
            "p_dropped_chunks": dropped_chunks,
            "p_total_qa": total_qa,
            "p_embedded_chunks": embedded_chunks,
            "p_embedded_qa": embedded_qa,
            "p_total_stories": total_stories,
            "p_embedded_stories": embedded_stories,
        }
        if status:
            params["p_status"] = status
        if stage_detail:
            params["p_stage_detail"] = stage_detail[:500]
        try:
            result = self.rpc("bump_dm_job_progress", params)
        except DatabaseError as exc:
            # 进度上报失败不该拖垮正在跑的业务任务
            logger.warning("任务进度上报失败 job=%s: %s", job_id, exc)
            return None
        if isinstance(result, list):
            return result[0] if result else None
        return result

    def fail_job(self, job_id: str, message: str) -> None:
        self.update_job(
            job_id,
            {
                "status": JOB_FAILED,
                "error_message": message[:2000],
                "finished_at": "now()",
            },
        )

    # ---------------- 分块 ----------------
    def insert_chunks(self, rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """批量写入 chunk（含向量），按 (document_id, content_hash) 去重。

        用 ``ignore-duplicates`` 而非 merge：重跑时同一段文本的向量不会变，
        没必要浪费一次写放大；返回体里拿到的仍是既有行的 id，外键照样能挂。
        """
        if not rows:
            return []
        resp = self._request(
            "POST",
            f"/{TABLE_CHUNKS}",
            params={"on_conflict": "document_id,content_hash"},
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            json=list(rows),
        )
        return self._rows(resp)

    def insert_qa(self, rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not rows:
            return []
        # 同批次内若同一 question_hash 出现多次（模型在同一批里重复产出相同问句，
        # 多见于「每片段多生成」之后），PostgREST 的单条 ON CONFLICT DO UPDATE
        # 会报「cannot affect row a second time」直接 500，拖垮整条流水线。
        # 入库前先按 question_hash 去重（保留第一条）；跨批次的重复由 on_conflict 兜底合并。
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
        resp = self._request(
            "POST",
            f"/{TABLE_QA}",
            params={"on_conflict": "document_id,question_hash"},
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            json=list(uniq),
        )
        return self._rows(resp)

    def count_chunks(self, document_id: str) -> int:
        resp = self._request(
            "GET",
            f"/{TABLE_CHUNKS}",
            params={"document_id": f"eq.{document_id}", "select": "id"},
            headers={"Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"},
        )
        return _parse_content_range(resp.headers.get("Content-Range"))

    def count_qa(self, document_id: str) -> int:
        resp = self._request(
            "GET",
            f"/{TABLE_QA}",
            params={"document_id": f"eq.{document_id}", "select": "id"},
            headers={"Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"},
        )
        return _parse_content_range(resp.headers.get("Content-Range"))

    # ---------------- 故事还原 ----------------
    def insert_stories(self, rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """批量写入故事还原条目，按 (document_id, content_hash) 去重。

        与 insert_qa 同策略：同批次内先按 content_hash 去重（避免 PostgREST
        单条 ON CONFLICT 报「cannot affect row a second time」），跨批次由
        on_conflict 兜底合并。
        """
        if not rows:
            return []
        seen: set = set()
        uniq: List[Dict[str, Any]] = []
        for r in rows:
            key = r.get("content_hash")
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        if len(uniq) != len(rows):
            logger.info("insert_stories 同批次去重：%s -> %s 条", len(rows), len(uniq))
        resp = self._request(
            "POST",
            f"/{TABLE_STORIES}",
            params={"on_conflict": "document_id,content_hash"},
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            json=list(uniq),
        )
        return self._rows(resp)

    def count_stories(self, document_id: str) -> int:
        resp = self._request(
            "GET",
            f"/{TABLE_STORIES}",
            params={"document_id": f"eq.{document_id}", "select": "id"},
            headers={"Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"},
        )
        return _parse_content_range(resp.headers.get("Content-Range"))

    def list_stories(
        self,
        *,
        script_code: str,
        story_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[Dict[str, Any]], int]:
        """按剧本聚合故事还原条目（仅活跃文档），返回 (items, total)。

        走 SQL 函数 list_dm_stories：需要过滤「文档 is_active」并按 story 聚合
        公开划线数，PostgREST 的平铺查询做不到，函数里一次算好。
        """
        result = self.rpc(
            "list_dm_stories",
            {
                "p_script_code": script_code,
                "p_story_type": story_type,
                "p_limit": limit,
                "p_offset": offset,
            },
        )
        if isinstance(result, dict):
            items = result.get("items") or []
            total = int(result.get("total") or 0)
            return items if isinstance(items, list) else [], total
        return [], 0

    def get_story(self, story_id: str) -> Optional[Dict[str, Any]]:
        resp = self._request(
            "GET", f"/{TABLE_STORIES}", params={"id": f"eq.{story_id}", "select": "*", "limit": 1}
        )
        rows = self._rows(resp)
        return rows[0] if rows else None

    def get_stories_by_ids(
        self, story_ids: Sequence[str]
    ) -> Dict[str, Dict[str, Any]]:
        """按 id 批量取故事条目的标题/类型，返回 {story_id: row}。

        共读时间线要展示「每条划线挂在哪个故事条目上」，但划线表是平铺的、
        没有冗余条目标题；一次 ``id=in.(...)`` 查询覆盖整页划线，避免逐条 N+1。
        """
        ids = [sid for sid in dict.fromkeys(story_ids) if sid]
        if not ids:
            return {}
        resp = self._request(
            "GET",
            f"/{TABLE_STORIES}",
            params={
                "id": f"in.({','.join(ids)})",
                "select": "id,title,story_type",
            },
        )
        return {str(r.get("id")): r for r in self._rows(resp) if r.get("id")}

    def reanchor_highlights(self, document_id: str) -> Optional[Dict[str, Any]]:
        """把 orphaned 划线按 quote 重新挂回新生成的故事条目。

        force 重跑 / 换新版本手册后，旧 story 被 purge 删除、划线标记为
        orphaned；T4 写完新 story 后调这个 RPC 重锚定。返回 {relined, still_orphaned}。
        """
        try:
            result = self.rpc("reanchor_dm_highlights", {"p_document_id": document_id})
        except DatabaseError as exc:
            # 重锚失败只是让部分划线暂时停留在 orphaned 状态，不该拖垮收尾
            logger.warning("划线重锚定失败 doc=%s: %s", document_id, exc)
            return None
        if isinstance(result, list):
            return result[0] if result else None
        return result

    # ---------------- 划线评论 ----------------
    def count_highlights(self, story_id: str, *, user_id: Optional[str] = None) -> int:
        params: Dict[str, Any] = {
            "story_id": f"eq.{story_id}",
            "deleted_at": "is.null",
            "select": "id",
        }
        if user_id:
            params["user_id"] = f"eq.{user_id}"
        resp = self._request(
            "GET",
            f"/{TABLE_HIGHLIGHTS}",
            params=params,
            headers={"Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"},
        )
        return _parse_content_range(resp.headers.get("Content-Range"))

    def create_highlight(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._request(
            "POST",
            f"/{TABLE_HIGHLIGHTS}",
            headers={"Prefer": "return=representation"},
            json=[payload],
        )
        rows = self._rows(resp)
        if not rows:
            raise DatabaseError("划线评论写入后未返回数据")
        return rows[0]

    def get_highlight(self, highlight_id: str) -> Optional[Dict[str, Any]]:
        resp = self._request(
            "GET", f"/{TABLE_HIGHLIGHTS}", params={"id": f"eq.{highlight_id}", "select": "*", "limit": 1}
        )
        rows = self._rows(resp)
        return rows[0] if rows else None

    def list_highlights(
        self,
        *,
        script_code: Optional[str] = None,
        story_id: Optional[str] = None,
        user_id: Optional[str] = None,
        visibility: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[List[Dict[str, Any]], int]:
        """列出划线评论（组合过滤），返回 (rows, total)。

        按需组合过滤维度：剧本（script_code）、条目（story_id）、作者（user_id）、
        可见性（visibility）。总数走 Content-Range，与 list_questions 同款解析。
        """
        params: Dict[str, Any] = {
            "deleted_at": "is.null",
            "select": "*",
            "order": "created_at.desc",
            "limit": limit,
            "offset": offset,
        }
        if script_code:
            params["script_code"] = f"eq.{script_code}"
        if story_id:
            params["story_id"] = f"eq.{story_id}"
        if user_id:
            params["user_id"] = f"eq.{user_id}"
        if visibility:
            params["visibility"] = f"eq.{visibility}"
        resp = self._request(
            "GET",
            f"/{TABLE_HIGHLIGHTS}",
            params=params,
            headers={"Prefer": "count=exact"},
        )
        total = _parse_content_range(resp.headers.get("Content-Range"))
        return self._rows(resp), total

    def update_highlight(self, highlight_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        resp = self._request(
            "PATCH",
            f"/{TABLE_HIGHLIGHTS}",
            params={"id": f"eq.{highlight_id}"},
            headers={"Prefer": "return=representation"},
            json=patch,
        )
        rows = self._rows(resp)
        return rows[0] if rows else None

    def soft_delete_highlight(self, highlight_id: str) -> None:
        """软删划线：deleted_at 置 now，列表与计数一律按 deleted_at is null 过滤。"""
        self._request(
            "PATCH",
            f"/{TABLE_HIGHLIGHTS}",
            params={"id": f"eq.{highlight_id}"},
            headers={"Prefer": "return=minimal"},
            json={"deleted_at": "now()"},
        )

    def list_qa_titles(self, script_code: str) -> List[Dict[str, Any]]:
        """按业务 code 取全部 QA（含标题与章节路径），行文顺序输出。

        行序即手册的原始行文顺序（文档创建时间 → 块序号 → QA 创建时间），
        由 SQL 函数 list_dm_qa_titles 保证，应用层直接按 section_path 组装标题树。
        """
        result = self.rpc("list_dm_qa_titles", {"p_script_code": script_code})
        return result if isinstance(result, list) else []

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
        result = self.rpc(
            "match_dm_chunks",
            {
                "query_embedding": to_pgvector(embedding),
                "p_script_id": script_id,
                "p_script_code": script_code,
                "p_document_id": document_id,
                "match_count": match_count,
                "similarity_threshold": similarity_threshold,
            },
        )
        return result if isinstance(result, list) else []

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
        result = self.rpc(
            "match_dm_qa",
            {
                "query_embedding": to_pgvector(embedding),
                "p_script_id": script_id,
                "p_script_code": script_code,
                "p_document_id": document_id,
                "p_category": category,
                "match_count": match_count,
                "similarity_threshold": similarity_threshold,
            },
        )
        return result if isinstance(result, list) else []

    # ---------------- 用户提问沉淀 ----------------
    def get_profiles(self, user_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """按 id 批量取用户资料（profiles 表），返回 {user_id: row}。

        提问记录只存 user id，昵称/头像在读取时由服务层批量关联合并 ——
        一次 ``id=in.(...)`` 查询覆盖整页记录，永远拿到最新资料，
        不需要任何冗余快照与异步同步。
        """
        ids = [uid for uid in dict.fromkeys(user_ids) if uid]
        if not ids:
            return {}
        resp = self._request(
            "GET",
            "/profiles",
            params={
                "id": f"in.({','.join(ids)})",
                "select": "id,nickname,avatar_url,avatar_color",
            },
        )
        return {str(r.get("id")): r for r in self._rows(resp) if r.get("id")}

    def record_question(
        self,
        *,
        script_id: str,
        script_code: str,
        question: str,
        question_hash: str,
        best_similarity: float = 0.0,
        created_by: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """记录一条低相似度提问（幂等 + 原子计数）。

        去重与计数下推到 SQL 函数 ``record_dm_question``：同一剧本同一问题
        只存一行，重复提问 ask_count 原子 +1 —— 两个用户同时问同一问题时，
        应用层「先查再插」会写出重复行或丢一次计数。
        """
        result = self.rpc(
            "record_dm_question",
            {
                "p_script_id": script_id,
                "p_script_code": script_code,
                "p_question": question,
                "p_question_hash": question_hash,
                "p_best_similarity": best_similarity,
                "p_created_by": created_by,
            },
        )
        if isinstance(result, list):
            return result[0] if result else None
        return result

    def get_question(self, question_id: str) -> Optional[Dict[str, Any]]:
        resp = self._request(
            "GET",
            f"/{TABLE_QUESTIONS}",
            params={"id": f"eq.{question_id}", "select": "*", "limit": 1},
        )
        rows = self._rows(resp)
        return rows[0] if rows else None

    def list_questions(
        self,
        *,
        script_code: str,
        status_filter: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[List[Dict[str, Any]], int]:
        """按剧本维度列出提问记录，默认人气优先（ask_count 倒序）。

        返回 (当前页行, 总条数)。总数走 PostgREST 的 Content-Range，
        与 count_chunks 同款解析方式，避免再发一次 count 请求。
        """
        params: Dict[str, Any] = {
            "script_code": f"eq.{script_code}",
            "select": "*",
            "order": "ask_count.desc,created_at.desc",
            "limit": limit,
            "offset": offset,
        }
        if status_filter:
            params["status"] = f"eq.{status_filter}"
        resp = self._request(
            "GET",
            f"/{TABLE_QUESTIONS}",
            params=params,
            headers={"Prefer": "count=exact"},
        )
        total = _parse_content_range(resp.headers.get("Content-Range"))
        return self._rows(resp), total

    def answer_question(
        self, question_id: str, *, answer: str, answered_by: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """真人解答：写入答案并转为 answered 态。

        只允许从 pending / answered（改答案）进入 answered；
        dismissed 的记录被视为无效问题，先回到 pending 才能解答 ——
        由服务层显式控制，这里不静默翻状态。
        """
        resp = self._request(
            "PATCH",
            f"/{TABLE_QUESTIONS}",
            params={"id": f"eq.{question_id}"},
            headers={"Prefer": "return=representation"},
            json={
                "answer": answer,
                "answered_by": answered_by,
                "answered_at": "now()",
                "status": QUESTION_ANSWERED,
            },
        )
        rows = self._rows(resp)
        return rows[0] if rows else None

    def list_guide_questions(
        self, script_code: str, *, limit: int = 3
    ) -> List[Dict[str, Any]]:
        """剧本维度的引导问题：按人气取前 N 条（已解答的优先展示价值最高）。

        排序规则：已解答的排前面（有答案可直接展示），同状态内按被问次数倒序。
        """
        resp = self._request(
            "GET",
            f"/{TABLE_QUESTIONS}",
            params={
                "script_code": f"eq.{script_code}",
                "status": f"in.({QUESTION_ANSWERED},{QUESTION_PENDING})",
                "select": "*",
                # PostgREST 多列排序：先按状态（answered 字典序在 pending 前），再按人气
                "order": f"status.asc,ask_count.desc,created_at.desc",
                "limit": limit,
            },
        )
        return self._rows(resp)


def _parse_content_range(value: Optional[str]) -> int:
    """解析 PostgREST 的 ``Content-Range: 0-0/123`` 取总数。"""
    if not value or "/" not in value:
        return 0
    total = value.rsplit("/", 1)[-1]
    return int(total) if total.isdigit() else 0


_store: Optional[DMStore] = None


def get_dm_store() -> DMStore:
    """进程内单例（同 LLM 客户端，prefork 后每个子进程各持一份）。"""
    global _store
    if _store is None:
        _store = DMStore()
    return _store


def reset_dm_store() -> None:
    global _store
    if _store is not None:
        _store.close()
    _store = None
