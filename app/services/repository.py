"""数据仓储层：把 PostgREST 查询语法收敛在这里，业务层只面向语义方法。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.services.supabase import SupabaseClient, get_supabase

TABLE_TASKS = "upload_tasks"
TABLE_PARTS = "upload_parts"
TABLE_FILES = "files"
TABLE_OPTION_CATEGORIES = "script_option_categories"
TABLE_OPTIONS = "script_options"
TABLE_SCRIPTS = "scripts"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UploadTaskRepository:
    def __init__(self, db: Optional[SupabaseClient] = None) -> None:
        self.db = db or get_supabase()

    async def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self.db.insert(TABLE_TASKS, payload)

    async def get(self, task_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        filters = {"id": f"eq.{task_id}"}
        if user_id:
            filters["user_id"] = f"eq.{user_id}"
        return await self.db.select_one(TABLE_TASKS, filters=filters)

    async def find_resumable(
        self, user_id: str, file_hash: str, file_size: int, key_prefix: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """查找可续传的任务：同一用户 + 同一内容指纹 + 同样大小且仍在上传中。

        key_prefix 非空时只在对应命名空间内匹配（如 temp/），避免续传到
        其他存储类型的对象（临时对象可能被生命周期规则清理，不应被永久上传复用）。
        """
        if not file_hash:
            return None
        filters = {
            "user_id": f"eq.{user_id}",
            "file_hash": f"eq.{file_hash}",
            "file_size": f"eq.{file_size}",
            "status": "eq.uploading",
        }
        if key_prefix:
            filters["object_key"] = f"like.{key_prefix}/*"
        rows = await self.db.select(
            TABLE_TASKS,
            filters=filters,
            order="created_at.desc",
            limit=1,
        )
        return rows[0] if rows else None

    async def list_by_user(
        self,
        user_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        filters = {"user_id": f"eq.{user_id}"}
        if status:
            filters["status"] = f"eq.{status}"
        return await self.db.select_with_count(
            TABLE_TASKS, filters=filters, order="created_at.desc", limit=limit, offset=offset
        )

    async def update(self, task_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        data = {**data, "updated_at": _now()}
        rows = await self.db.update(TABLE_TASKS, filters={"id": f"eq.{task_id}"}, data=data)
        return rows[0] if rows else None

    async def mark_completed(self, task_id: str) -> None:
        await self.update(task_id, {"status": "completed", "completed_at": _now()})

    async def mark_failed(self, task_id: str, reason: str) -> None:
        await self.update(task_id, {"status": "failed", "error_message": reason[:500]})

    async def mark_aborted(self, task_id: str) -> None:
        await self.update(task_id, {"status": "aborted"})


class UploadPartRepository:
    def __init__(self, db: Optional[SupabaseClient] = None) -> None:
        self.db = db or get_supabase()

    async def record(self, task_id: str, part_number: int, etag: str, size: int) -> Dict[str, Any]:
        """幂等写入分片记录：同一分片重传会覆盖旧 ETag。"""
        rows = await self.db.upsert(
            TABLE_PARTS,
            {
                "task_id": task_id,
                "part_number": part_number,
                "etag": etag.strip('"'),
                "size": size,
                "created_at": _now(),
            },
            on_conflict="task_id,part_number",
        )
        return rows[0] if rows else {}

    async def record_many(self, task_id: str, parts: List[Dict[str, Any]]) -> None:
        if not parts:
            return
        payload = [
            {
                "task_id": task_id,
                "part_number": int(p["part_number"]),
                "etag": str(p["etag"]).strip('"'),
                "size": int(p.get("size") or 0),
                "created_at": _now(),
            }
            for p in parts
        ]
        await self.db.upsert(TABLE_PARTS, payload, on_conflict="task_id,part_number")

    async def list_by_task(self, task_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.select(
            TABLE_PARTS, filters={"task_id": f"eq.{task_id}"}, order="part_number.asc"
        )
        return rows

    async def delete_by_task(self, task_id: str) -> None:
        await self.db.delete(TABLE_PARTS, filters={"task_id": f"eq.{task_id}"})


class FileRepository:
    def __init__(self, db: Optional[SupabaseClient] = None) -> None:
        self.db = db or get_supabase()

    async def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self.db.insert(TABLE_FILES, payload)

    async def get(self, file_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        filters = {"id": f"eq.{file_id}", "deleted_at": "is.null"}
        if user_id:
            filters["user_id"] = f"eq.{user_id}"
        return await self.db.select_one(TABLE_FILES, filters=filters)

    async def get_by_task(self, task_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        return await self.db.select_one(
            TABLE_FILES,
            filters={
                "task_id": f"eq.{task_id}",
                "user_id": f"eq.{user_id}",
                "deleted_at": "is.null",
            },
        )

    async def find_by_hash(
        self, user_id: str, file_hash: str, key_prefix: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """秒传依据：同用户下已存在相同内容指纹的文件。

        key_prefix 非空时只在对应命名空间内匹配（如 temp/），避免临时文件被
        永久上传以秒传形式复用——否则临时对象被生命周期规则删除后会拖垮永久文件。
        """
        if not file_hash:
            return None
        filters = {
            "user_id": f"eq.{user_id}",
            "file_hash": f"eq.{file_hash}",
            "deleted_at": "is.null",
        }
        if key_prefix:
            filters["object_key"] = f"like.{key_prefix}/*"
        rows = await self.db.select(
            TABLE_FILES,
            filters=filters,
            order="created_at.desc",
            limit=1,
        )
        return rows[0] if rows else None

    async def list_by_user(
        self,
        user_id: str,
        *,
        keyword: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        filters = {"user_id": f"eq.{user_id}", "deleted_at": "is.null"}
        # 头像走 simple_upload(prefix=avatars) 时也会落一条 files 记录，
        # 但它由 profiles.avatar_object_key 单独管理，不应出现在用户的文件列表里。
        filters["object_key"] = "not.like.avatars/*"
        if keyword:
            safe = keyword.replace("*", "").replace(",", "")
            filters["filename"] = f"ilike.*{safe}*"
        return await self.db.select_with_count(
            TABLE_FILES, filters=filters, order="created_at.desc", limit=limit, offset=offset
        )

    async def soft_delete(self, file_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        rows = await self.db.update(
            TABLE_FILES,
            filters={"id": f"eq.{file_id}", "user_id": f"eq.{user_id}", "deleted_at": "is.null"},
            data={"deleted_at": _now(), "updated_at": _now()},
        )
        return rows[0] if rows else None

    async def count_references(self, object_key: str) -> int:
        """统计仍指向同一对象的未删除记录数，用于判断能否物理删除 OSS 对象。"""
        _, total = await self.db.select_with_count(
            TABLE_FILES,
            filters={"object_key": f"eq.{object_key}", "deleted_at": "is.null"},
            columns="id",
            limit=1,
        )
        return total


def _escape_like(keyword: str) -> str:
    """清理 PostgREST 模糊查询关键词。

    `*` 是 PostgREST 的通配符、`,` 与括号会破坏 or=() 的语法结构，一律剔除。
    """
    for ch in ("*", ",", "(", ")", '"', "\\"):
        keyword = keyword.replace(ch, "")
    return keyword.strip()


# 文件名扩展名：导入时标题常从文件名派生，需剥掉
_TITLE_EXT_RE = re.compile(r"\.(pdf|docx?|txt|epub|pptx?|md|rtf)$", re.IGNORECASE)
# 操作系统复制文件时自动追加的噪音：「副本」「copy」「（1）」
_TITLE_DUP_RE = re.compile(r"\s*(副本|copy)$", re.IGNORECASE)
_TITLE_PAREN_NUM_RE = re.compile(r"[（(]\s*[0-9]+\s*[）)]\s*$")


def normalize_title_key(title: str) -> str:
    """把剧本标题归一化成「同一本剧本」的稳定匹配键，用于导入去重。

    只清掉系统噪音，不清用户有意写的序号：
      - 去首尾空白、转小写；
      - 剥文件名扩展名（.pdf/.docx...）；
      - 去掉操作系统复制文件自动加的「副本 / copy /（1）」等。

    于是「山母鬼」「山母鬼.pdf」「山母鬼 副本」「山母鬼（1）」都被识别为同一本，
    重传时合并回原行，而不是生成 shan-mu-gui-2 这种假续集编码。

    刻意**保留**「山母鬼2」「山母鬼 第二部」等用户有意序号，避免把真续集误判成重传。
    """
    if not title:
        return ""
    key = title.strip().lower()
    key = _TITLE_EXT_RE.sub("", key)
    # 反复清理，兼容「山母鬼 副本（1）」这类噪音叠加
    for _ in range(3):
        new = _TITLE_PAREN_NUM_RE.sub("", key)
        new = _TITLE_DUP_RE.sub("", new).strip()
        if new == key:
            break
        key = new
    return key


class ScriptOptionRepository:
    """剧本杀筛选维度字典的读写。

    字典表没有 user_id，是全局共享的只读参考数据，
    因此这里不需要像业务表那样强制带用户过滤。
    """

    def __init__(self, db: Optional[SupabaseClient] = None) -> None:
        self.db = db or get_supabase()

    # ---------------- 读 ----------------
    async def list_categories(self, *, include_inactive: bool = False) -> List[Dict[str, Any]]:
        filters: Dict[str, str] = {}
        if not include_inactive:
            filters["is_active"] = "is.true"
        return await self.db.select(
            TABLE_OPTION_CATEGORIES,
            filters=filters,
            order="sort_order.asc,code.asc",
        )

    async def get_category(self, code: str) -> Optional[Dict[str, Any]]:
        return await self.db.select_one(
            TABLE_OPTION_CATEGORIES,
            filters={"code": f"eq.{code}", "is_active": "is.true"},
        )

    async def list_options(
        self,
        *,
        category_code: Optional[str] = None,
        only_hot: bool = False,
        keyword: Optional[str] = None,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        filters: Dict[str, str] = {}
        if not include_inactive:
            filters["is_active"] = "is.true"
        if category_code:
            filters["category_code"] = f"eq.{category_code}"
        if only_hot:
            filters["is_hot"] = "is.true"
        if keyword:
            safe = _escape_like(keyword)
            if safe:
                # 标签、说明、口语别名三处任一命中即可
                filters["or"] = (
                    f"(label.ilike.*{safe}*,description.ilike.*{safe}*,aliases.cs.{{{safe}}})"
                )
        return await self.db.select(
            TABLE_OPTIONS,
            filters=filters,
            order="category_code.asc,sort_order.asc,code.asc",
        )

    # ---------------- 写（仅灌数据脚本使用） ----------------
    async def upsert_categories(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not rows:
            return []
        return await self.db.upsert(TABLE_OPTION_CATEGORIES, rows, on_conflict="code")

    async def upsert_options(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not rows:
            return []
        return await self.db.upsert(TABLE_OPTIONS, rows, on_conflict="category_code,code")


def _pg_array(values: List[str]) -> str:
    """把字符串列表拼成 PostgREST 的数组字面量：{a,b,c}。

    元素里的逗号与花括号会破坏字面量结构，一律剔除 —— 字典 code 本就是
    纯英文 slug，正常数据不会被影响。
    """
    safe = [v.replace(",", "").replace("{", "").replace("}", "").strip() for v in values]
    return "{" + ",".join(v for v in safe if v) + "}"


class ScriptRepository:
    """剧本库读写。

    剧本是**全站共享的公开内容**（不是用户私有数据），所以这里不带 user_id 过滤；
    但所有读操作都强制排除软删除记录，避免已下线的剧本从列表漏出去。
    """

    # 允许前端指定的排序方式，白名单防止 order 参数注入
    SORTS = {
        "hot": "play_count.desc,rating.desc.nullslast",
        "rating": "rating.desc.nullslast,play_count.desc",
        "newest": "created_at.desc",
        "year": "published_year.desc.nullslast,play_count.desc",
        "title": "title.asc",
    }

    def __init__(self, db: Optional[SupabaseClient] = None) -> None:
        self.db = db or get_supabase()

    # ---------------- 读 ----------------
    async def get(self, script_id: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        filters = {"id": f"eq.{script_id}"}
        if not include_deleted:
            filters["deleted_at"] = "is.null"
        return await self.db.select_one(TABLE_SCRIPTS, filters=filters)

    async def get_by_code(self, code: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        filters = {"code": f"eq.{code}"}
        if not include_deleted:
            filters["deleted_at"] = "is.null"
        return await self.db.select_one(TABLE_SCRIPTS, filters=filters)

    async def list_scripts(
        self,
        *,
        keyword: Optional[str] = None,
        playstyles: Optional[List[str]] = None,
        themes: Optional[List[str]] = None,
        release_types: Optional[List[str]] = None,
        difficulties: Optional[List[str]] = None,
        players: Optional[int] = None,
        duration: Optional[int] = None,
        min_rating: Optional[float] = None,
        recommended_only: bool = False,
        status: Optional[str] = "published",
        created_by: Optional[str] = None,
        sort: str = "hot",
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        filters: Dict[str, str] = {"deleted_at": "is.null"}
        if status:
            filters["status"] = f"eq.{status}"
        # 「我的剧本」：只返回当前用户创建的记录。
        # 后端用 service_role 绕过 RLS，这里必须显式带 created_by，否则会漏掉
        # 草稿 / 下架等 RLS 不可见的记录。
        if created_by:
            filters["created_by"] = f"eq.{created_by}"

        # 数组维度用 ov（overlap）：命中任意一个即可，符合筛选器多选的直觉
        if playstyles:
            filters["playstyles"] = f"ov.{_pg_array(playstyles)}"
        if themes:
            filters["themes"] = f"ov.{_pg_array(themes)}"

        # 标量维度多选走 in.()
        if release_types:
            filters["release_type"] = f"in.({','.join(release_types)})"
        if difficulties:
            filters["difficulty"] = f"in.({','.join(difficulties)})"

        # 人数/时长：字典选项的区间落在剧本区间内即算命中
        if players is not None:
            filters["player_min"] = f"lte.{players}"
            filters["player_max"] = f"gte.{players}"
        if duration is not None:
            filters["duration_min"] = f"lte.{duration}"
            filters["duration_max"] = f"gte.{duration}"

        if min_rating is not None:
            filters["rating"] = f"gte.{min_rating}"
        if recommended_only:
            filters["is_recommended"] = "is.true"

        if keyword:
            safe = _escape_like(keyword)
            if safe:
                filters["or"] = (
                    f"(title.ilike.*{safe}*,summary.ilike.*{safe}*,"
                    f"author.ilike.*{safe}*,publisher.ilike.*{safe}*,"
                    f"aliases.cs.{{{safe}}})"
                )

        order = self.SORTS.get(sort, self.SORTS["hot"])
        return await self.db.select_with_count(
            TABLE_SCRIPTS, filters=filters, order=order, limit=limit, offset=offset
        )

    async def search_by_name(
        self, name: str, *, status: Optional[str] = "published", limit: int = 10
    ) -> List[Dict[str, Any]]:
        """按名称（标题 / 别名）模糊召回候选剧本。

        只匹配 `title` 与 `aliases`，不碰 `summary` / `author` / `publisher`，
        契合「按名字找剧本」的语义，避免简介里的关键词误召回一堆无关剧本。
        精确/前缀/包含的排序交给 service 层在内存里做，这里只负责召回。

        `status` 默认只查已上架剧本；传 `None` 则不过滤状态（仍排除软删除）。
        """
        safe = _escape_like(name).replace("{", "").replace("}", "")
        if not safe:
            return []
        filters: Dict[str, str] = {"deleted_at": "is.null"}
        if status:
            filters["status"] = f"eq.{status}"
        # title 模糊 + 别名精确命中（数组 contains）
        filters["or"] = f"(title.ilike.*{safe}*,aliases.cs.{{{safe}}})"
        return await self.db.select(
            TABLE_SCRIPTS,
            filters=filters,
            order="play_count.desc,rating.desc.nullslast",
            limit=max(1, min(limit, 50)),
        )

    async def find_existing(self, title: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        """按名称去重查找：用于导入时判断剧本库是否已有该剧本。

        匹配规则（与「按名称找剧本」语义一致，但只取精确命中，供关联使用）：
        - 标题精确匹配（忽略大小写，先清理特殊字符避免破坏 `or=()` 语法）；
        - 或别名数组精确包含该名称；
        - 上面两者都用「归一化键」再过一遍：剥掉扩展名 / 首尾空白 / 系统复制噪音，
          于是「山母鬼」「山母鬼.pdf」「山母鬼 副本」都命中同一条，重传时合并回原行，
          避免生成 shan-mu-gui-2 这类假续集编码。

        排除了软删除记录。命中多个时取热度最高的一条，导入关联落到最可能被
        用户认领的那份上。找不到返回 `None`。
        """
        raw = (title or "").strip()
        if not raw:
            return None
        key = normalize_title_key(title)
        if not key:
            return None
        esc_raw = _escape_like(raw)
        esc_key = _escape_like(key)
        filters: Dict[str, str] = {}
        if not include_deleted:
            filters["deleted_at"] = "is.null"
        # 归一化键同时匹配标题与别名，让同一本剧本的不同标题写法都能关联
        filters["or"] = (
            f"(title.ilike.{esc_raw},"
            f"title.ilike.{esc_key},"
            f"aliases.cs.{{{esc_key}}})"
        )
        rows = await self.db.select(
            TABLE_SCRIPTS,
            filters=filters,
            order="play_count.desc,rating.desc.nullslast",
            limit=5,
        )
        return rows[0] if rows else None

    async def autocomplete(
        self, q: str, *, status: Optional[str] = "published", limit: int = 8
    ) -> List[Dict[str, Any]]:
        """自动补全（联想）用的轻量检索。

        只查已上架（默认）且未删除的剧本，按「标题模糊 + 别名精确」召回，
        只回选中的少数几列（不含 summary / extra 大字段之外的冗余信息），
        适配下拉框边输入边查的实时场景。返回行供 service 层映射成轻量项。
        """
        safe = _escape_like(q)
        if not safe:
            return []
        filters: Dict[str, str] = {"deleted_at": "is.null"}
        if status:
            filters["status"] = f"eq.{status}"
        filters["or"] = f"(title.ilike.*{safe}*,aliases.cs.{{{safe}}})"
        columns = "id,code,title,author,cover_url,aliases,extra,status"
        return await self.db.select(
            TABLE_SCRIPTS,
            filters=filters,
            columns=columns,
            order="play_count.desc,rating.desc.nullslast",
            limit=max(1, min(limit, 20)),
        )

    async def count_all(self) -> int:
        _, total = await self.db.select_with_count(
            TABLE_SCRIPTS, filters={"deleted_at": "is.null"}, columns="id", limit=1
        )
        return total

    # ---------------- 写 ----------------
    async def create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self.db.insert(TABLE_SCRIPTS, payload)

    async def update(self, script_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        data = {**data, "updated_at": _now()}
        rows = await self.db.update(
            TABLE_SCRIPTS,
            filters={"id": f"eq.{script_id}", "deleted_at": "is.null"},
            data=data,
        )
        return rows[0] if rows else None

    async def update_by_code(
        self, code: str, data: Dict[str, Any], *, include_deleted: bool = False
    ) -> Optional[Dict[str, Any]]:
        """按 code 更新（用于撞 `uq_scripts_code` 唯一约束时把提交「补充」到已有行）。

        ``include_deleted=True`` 时不过滤软删除，并在更新时把 ``deleted_at`` 置空，
        从而「复活」因竞态被软删的重复记录，避免并发下仍报唯一约束错误。
        """
        payload = {**data, "updated_at": _now()}
        filters: Dict[str, str] = {"code": f"eq.{code}"}
        if include_deleted:
            payload["deleted_at"] = None
        else:
            filters["deleted_at"] = "is.null"
        rows = await self.db.update(TABLE_SCRIPTS, filters=filters, data=payload)
        return rows[0] if rows else None

    async def soft_delete(self, script_id: str) -> Optional[Dict[str, Any]]:
        rows = await self.db.update(
            TABLE_SCRIPTS,
            filters={"id": f"eq.{script_id}", "deleted_at": "is.null"},
            data={"deleted_at": _now(), "updated_at": _now(), "status": "offline"},
        )
        return rows[0] if rows else None

    async def upsert_many(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 code 幂等写入，灌数据脚本可重复执行。"""
        if not rows:
            return []
        return await self.db.upsert(TABLE_SCRIPTS, rows, on_conflict="code")
