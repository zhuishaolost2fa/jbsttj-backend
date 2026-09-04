"""剧本「求解析」诉求的业务层。

核心语义：

1. **去重**：同一用户对同一剧本只能求一次 —— 库中剧本按 script_id、
   库外剧本按归一化标题键（normalize_title_key）作为 match_key 去重；
2. **幂等**：重复发起返回已有记录（``already_exists=true``），已取消的
   记录直接「复活」回 pending，不新建行；
3. **已完成自动流转**：读取列表 / 排行榜前惰性对照
   ``script_dm_documents``（is_active=true 且 total_chunks>0）判定剧本
   是否已解析，把对应 pending 诉求批量置 completed —— 解析流水线无需
   反向耦合本模块，状态始终在读取时自洽；
4. **取消是软取消**：置 cancelled，不删行，用户可再次发起（复活）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import get_settings
from app.core.exceptions import ConflictError, DatabaseError, NotFoundError, ValidationError
from app.services.notifier import get_notifier
from app.schemas.common import Pagination
from app.schemas.script_request import (
    ScriptRequestCreate,
    ScriptRequestItem,
    ScriptRequestLeaderboardItem,
    ScriptRequestLeaderboardResult,
    ScriptRequestListResult,
)
from app.services.repository import (
    ScriptRequestRepository,
    ScriptRepository,
    normalize_title_key,
)

logger = logging.getLogger("app.script_request")

# 与 sql/script_requests.sql 的唯一约束名保持一致
_UQ_USER_SCRIPT = "uq_script_requests_user_script"

STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_unique_violation(exc: DatabaseError, constraint: str) -> bool:
    """判断数据库异常是否由指定唯一约束冲突引起（同 script_service 的判定）。"""
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        text = f"{details.get('message', '')} {details.get('details', '')}"
    else:
        text = str(details)
    return constraint in text


class ScriptRequestService:
    def __init__(
        self,
        repo: Optional[ScriptRequestRepository] = None,
        scripts: Optional[ScriptRepository] = None,
    ) -> None:
        self.repo = repo or ScriptRequestRepository()
        self.scripts = scripts or ScriptRepository()

    # ================= 发起求解析 =================
    async def create(
        self,
        user_id: str,
        payload: ScriptRequestCreate,
        *,
        user_email: Optional[str] = None,
    ) -> ScriptRequestItem:
        """发起求解析。

        ``user_email`` 仅用于运营通知里标识发起人（缺省退化为 user_id 前 8 位），
        不参与任何业务逻辑。
        """
        title = (payload.script_title or "").strip()
        if not title:
            raise ValidationError("剧本名称不能为空", code="title_required")

        # 1) 定位目标剧本：id / code / 标题匹配，命中则统一以 script_id 作为去重键
        script = await self._resolve_script(payload, title)
        if script is not None:
            match_key = str(script["id"])
        else:
            match_key = normalize_title_key(title)
            if not match_key:
                raise ValidationError("无法识别的剧本名称", code="invalid_script_title")

        # 2) 剧本已解析则直接拦截：再求没有意义
        if script is not None and await self._is_indexed(str(script["id"])):
            raise ConflictError(
                "该剧本已解析完成，无需再次求解析",
                code="script_already_parsed",
            )

        # 3) 去重：同用户同剧本只有一条诉求
        existing = await self.repo.find_by_match_key(user_id, match_key)
        if existing is not None:
            return await self._reuse_with_notify(existing, match_key, user_id, user_email)

        # 4) 新建（并发撞唯一约束时内部会退化成「复用 / 复活」，
        #    由 already_exists 区分，避免两条路径重复通知）
        item = await self._insert_request(
            user_id, script, match_key, title, payload.reason, user_email=user_email
        )
        if not item.already_exists:
            logger.info("新增求解析 user=%s script=%s title=%s", user_id, match_key, title)
            self._fire_notify(item, match_key=match_key, user_id=user_id,
                              user_email=user_email, revived=False)
        return item

    async def _reuse_with_notify(
        self,
        existing: Dict[str, Any],
        match_key: str,
        user_id: str,
        user_email: Optional[str],
    ) -> ScriptRequestItem:
        """命中已有诉求时的一致性处理（复用 / 复活 / 拦截）＋ 复活通知。

        「复活」（cancelled → pending）也是一次新的求解析意愿，但同一用户
        反复取消-重开会刷屏，所以默认不推，由 NOTIFY_ON_REVIVE 显式打开。
        """
        was_cancelled = existing.get("status") == STATUS_CANCELLED
        item = await self._reuse_or_conflict(existing, match_key)
        if was_cancelled and item.status == STATUS_PENDING:
            self._fire_notify(item, match_key=match_key, user_id=user_id,
                              user_email=user_email, revived=True)
        return item

    # ================= 运营通知（旁路，失败不影响任何业务结果）=================
    def _fire_notify(
        self,
        item: ScriptRequestItem,
        *,
        match_key: str,
        user_id: str,
        user_email: Optional[str],
        revived: bool = False,
    ) -> None:
        """把「有人求解析」投递到后台任务，不等结果、不阻塞接口。

        用 ``asyncio.create_task`` 而非 FastAPI 的 BackgroundTasks：通知挂在
        service 层，这样无论从 HTTP 接口还是从内部调用 create，行为都一致。
        任务内部自行兜住所有异常 —— 求解析已经落库成功，推送失败不该让
        用户感知到（也不该让接口返回 500）。
        """
        settings = get_settings()
        if not settings.notify_enabled:
            return
        if revived and not settings.notify_on_revive:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 非异步上下文（如脚本直调）直接跳过
            logger.debug("无事件循环，跳过求解析通知")
            return
        loop.create_task(
            self._notify_async(
                item, match_key=match_key, user_id=user_id,
                user_email=user_email, revived=revived,
            )
        )

    async def _notify_async(
        self,
        item: ScriptRequestItem,
        *,
        match_key: str,
        user_id: str,
        user_email: Optional[str],
        revived: bool,
    ) -> None:
        """通知的实际执行体：补一个「累计多少人求过」，再交给 Notifier。"""
        try:
            count = 0
            try:
                count = await self._pending_count(match_key)
            except DatabaseError as exc:  # noqa: BLE001
                logger.warning("统计累计求解析人数失败，通知里省略该字段: %s", exc)
            result = await get_notifier().notify_script_request(
                script_title=item.script_title,
                script_id=item.script_id,
                script_code=item.script_code,
                reason=item.reason,
                user_id=user_id,
                user_email=user_email,
                in_library=bool(item.script_id),
                pending_count=count,
                match_key=match_key,
                revived=revived,
            )
            logger.info("求解析通知结果: %s", result)
        except Exception:  # noqa: BLE001 - 兜底：通知的任何异常都不许冒泡
            logger.exception("发送求解析通知时发生未预期异常")

    async def _pending_count(self, match_key: str) -> int:
        """同一剧本当前 pending 的诉求条数（含刚落库的这条）。"""
        rows = await self.repo.leaderboard_rows()
        return sum(1 for r in rows if (r.get("match_key") or "") == match_key)

    async def _insert_request(
        self,
        user_id: str,
        script: Optional[Dict[str, Any]],
        match_key: str,
        title: str,
        reason: Optional[str],
        *,
        user_email: Optional[str] = None,
    ) -> ScriptRequestItem:
        """落库一条新诉求，返回统一的出参模型。

        并发下可能撞 ``uq_script_requests_user_script`` 唯一约束，此时退化成
        「复用 / 复活」分支 —— 返回 ``already_exists=true`` 的 item。
        （早前这里两条分支的返回类型不一致：新建返回 dict、冲突返回 item，
        冲突路径上 ``_to_item`` 会拿不到 key 直接抛错，已统一为 item。）
        """
        data: Dict[str, Any] = {
            "user_id": user_id,
            "match_key": match_key,
            "script_title": title,
            "status": STATUS_PENDING,
            "reason": (reason or "").strip() or None,
        }
        if script is not None:
            data["script_id"] = str(script["id"])
            data["script_code"] = script.get("code") or None
            # 关联到库内剧本时回填真实标题：用户可能随手写了个变体标题，
            # 展示与榜单聚合都应以库内规范名称为准
            data["script_title"] = script.get("title") or title
        try:
            row = await self.repo.create(data)
        except DatabaseError as exc:
            # 并发撞唯一约束：读回既有行，走统一分支，绝不报 500
            if not _is_unique_violation(exc, _UQ_USER_SCRIPT):
                raise
            existing = await self.repo.find_by_match_key(user_id, match_key)
            if existing is None:
                raise
            return await self._reuse_with_notify(existing, match_key, user_id, user_email)
        return self._to_item(row, already_exists=False)

    async def _reuse_or_conflict(
        self, existing: Dict[str, Any], match_key: str
    ) -> ScriptRequestItem:
        """命中已有诉求时的分支：pending 复用 / cancelled 复活 / completed 拦截。"""
        status = existing.get("status")
        if status == STATUS_CANCELLED:
            row = await self.repo.update(
                str(existing["id"]),
                {
                    "status": STATUS_PENDING,
                    "cancelled_at": None,
                    "completed_at": None,
                },
            )
            logger.info("求解析复活 request=%s", existing["id"])
            return self._to_item(row or existing, already_exists=True)
        if status == STATUS_COMPLETED:
            raise ConflictError(
                "该剧本已解析完成，无需再次求解析",
                code="script_already_parsed",
            )
        # pending：幂等返回既有记录
        return self._to_item(existing, already_exists=True)

    async def _resolve_script(
        self, payload: ScriptRequestCreate, title: str
    ) -> Optional[Dict[str, Any]]:
        """解析目标剧本行；命中返回 scripts 行（id/code/title），未命中返回 None。

        优先级：scriptId > scriptCode > 按标题在库内匹配（导入去重同款规则，
        避免「库里已有同名剧本、又新增一条库外诉求」造成同一剧本两条诉求）。
        """
        if payload.script_id:
            row = await self.scripts.get(payload.script_id)
            if row is None:
                raise NotFoundError(
                    f"剧本不存在: {payload.script_id}", code="script_not_found"
                )
            return row
        if payload.script_code:
            row = await self.scripts.get_by_code(payload.script_code.lower())
            if row is None:
                raise NotFoundError(
                    f"剧本不存在: {payload.script_code}", code="script_not_found"
                )
            return row
        return await self.scripts.find_existing(title)

    async def _is_indexed(self, script_id: str) -> bool:
        """该剧本是否已完成解析（活跃文档 total_chunks>0）。

        判定失败时按「未解析」放行 —— 求解析不该被解析状态查询拖垮。
        """
        try:
            indexed = await self.repo.list_indexed_script_ids()
            return script_id in indexed
        except DatabaseError as exc:  # noqa: BLE001
            logger.warning("查询已解析剧本失败，按未解析放行: %s", exc)
            return False

    # ================= 取消求解析 =================
    async def cancel(self, user_id: str, request_id: str) -> ScriptRequestItem:
        row = await self.repo.get(request_id, user_id=user_id)
        if row is None:
            raise NotFoundError("求解析记录不存在", code="request_not_found")

        if row.get("status") == STATUS_COMPLETED:
            raise ConflictError(
                "该求解析已完成，不能取消", code="request_completed"
            )
        if row.get("status") == STATUS_CANCELLED:
            # 幂等：已取消则直接返回现状
            return self._to_item(row)

        updated = await self.repo.update(
            request_id, {"status": STATUS_CANCELLED, "cancelled_at": _now()}, user_id=user_id
        )
        logger.info("取消求解析 request=%s user=%s", request_id, user_id)
        return self._to_item(updated or row)

    # ================= 我的求解析列表 =================
    async def list_mine(
        self,
        user_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> ScriptRequestListResult:
        if status is not None:
            status = status.strip().lower()
            if status not in (STATUS_PENDING, STATUS_COMPLETED, STATUS_CANCELLED):
                raise ValidationError(
                    "status 只能是 pending / completed / cancelled", code="invalid_status"
                )
        await self._sync_completed()
        rows, total = await self.repo.list_by_user(
            user_id, status=status, limit=limit, offset=offset
        )
        items = await self._to_items(rows)
        return ScriptRequestListResult(
            items=items,
            pagination=Pagination(
                total=total,
                limit=limit,
                offset=offset,
                has_more=offset + len(items) < total,
            ),
        )

    # ================= 排行榜 =================
    async def leaderboard(
        self, *, limit: int = 20, offset: int = 0
    ) -> ScriptRequestLeaderboardResult:
        await self._sync_completed()
        rows = await self.repo.leaderboard_rows()

        # 内存聚合：按 match_key 分组（库内=script_id、库外=归一化标题键），
        # 每组统计诉求人数，并合并出代表行（script_id/script_code/规范标题优先非空）。
        # 说明：Supabase 托管 PostgREST 默认禁用服务端聚合函数（select 里写
        # count() 会报 PGRST123），且诉求表是用户量级，全量拉取在内存分组可行；
        # 按 match_key 分组也比 PostgREST 的 GROUP BY 全列更准 —— 同一剧本的
        # 不同标题变体会合并计数。
        groups: Dict[str, Dict[str, Any]] = {}
        counts: Dict[str, int] = {}
        for r in rows:
            key = r.get("match_key") or ""
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
            merged = groups.get(key)
            if merged is None:
                groups[key] = {
                    k: r.get(k) for k in ("script_id", "script_code", "script_title")
                }
                continue
            # 同组内合并：script_id / script_code 优先取非空；标题取更长（更规范）者
            for field in ("script_id", "script_code"):
                if not merged.get(field) and r.get(field):
                    merged[field] = r[field]
            if len(str(r.get("script_title") or "")) > len(str(merged.get("script_title") or "")):
                merged["script_title"] = r["script_title"]

        ranked = [{**row, "count": counts[key]} for key, row in groups.items()]
        # 诉求人数降序、同分按标题升序
        ranked.sort(
            key=lambda r: (
                -(int(r.get("count") or 0)),
                (r.get("script_title") or "").lower(),
            )
        )
        total = len(ranked)
        page = ranked[offset : offset + limit]

        script_ids = [str(r["script_id"]) for r in page if r.get("script_id")]
        script_map = {str(s["id"]): s for s in await self.scripts.get_scripts(script_ids)}

        items = [
            ScriptRequestLeaderboardItem(
                script_id=str(r["script_id"]) if r.get("script_id") else None,
                script_code=r.get("script_code") or None,
                script_title=r.get("script_title") or "",
                cover_url=(
                    script_map.get(str(r["script_id"]), {}).get("cover_url")
                    if r.get("script_id")
                    else None
                ),
                request_count=int(r.get("count") or 0),
            )
            for r in page
        ]
        return ScriptRequestLeaderboardResult(
            items=items,
            pagination=Pagination(
                total=total,
                limit=limit,
                offset=offset,
                has_more=offset + len(items) < total,
            ),
        )

    # ================= 惰性同步：剧本已解析 → 诉求置 completed =================
    async def _sync_completed(self) -> None:
        """把「剧本已解析完成」的 pending 诉求批量置为 completed。

        全程是可选增强：任何一步失败都只记日志、不影响列表 / 排行榜主流程。
        两段式：
        1. 已关联剧本（script_id 非空）的诉求：对照已解析剧本 ID 集合批量更新；
        2. 库外诉求（script_id 为空）：把已解析剧本的标题 / 别名与诉求的
           归一化标题键做内存匹配，命中则回填 script_id 并置 completed。
        """
        try:
            indexed_ids = await self.repo.list_indexed_script_ids()
        except DatabaseError as exc:  # noqa: BLE001
            logger.warning("同步已完成状态失败（无法读取已解析剧本）: %s", exc)
            return
        if not indexed_ids:
            return

        try:
            pending = await self.repo.list_pending()
            if not pending:
                return

            # 1) 直接按 script_id 批量完成
            direct_ids = [
                str(r["script_id"])
                for r in pending
                if r.get("script_id") and str(r["script_id"]) in indexed_ids
            ]
            if direct_ids:
                await self.repo.mark_completed_by_script_ids(direct_ids, _now())

            # 2) 库外诉求：按标题匹配已解析剧本，命中则回填并完成
            unlinked = [r for r in pending if not r.get("script_id")]
            if unlinked:
                await self._backfill_unlinked(unlinked, indexed_ids)
        except DatabaseError as exc:  # noqa: BLE001
            logger.warning("同步已完成状态失败（更新诉求表异常）: %s", exc)

    async def _backfill_unlinked(
        self, pending: List[Dict[str, Any]], indexed_ids: set
    ) -> None:
        """库外诉求 → 已解析剧本的标题匹配回填。

        用归一化标题键（与导入去重同款规则）对比已解析剧本的标题与别名，
        命中即说明「用户求的这本剧已被解析」，回填剧本身份并置 completed。
        """
        indexed_rows = await self.scripts.get_scripts(list(indexed_ids))
        # 已解析剧本 → 归一化标题键集合（标题 + 别名）
        key_to_script: Dict[str, Dict[str, Any]] = {}
        for s in indexed_rows:
            keys = [normalize_title_key(s.get("title") or "")]
            keys += [normalize_title_key(a) for a in (s.get("aliases") or [])]
            for k in keys:
                if k:
                    key_to_script.setdefault(k, s)

        for r in pending:
            script = key_to_script.get(r.get("match_key") or "")
            if script is None:
                continue
            updated = await self.repo.update(
                str(r["id"]),
                {
                    "script_id": str(script["id"]),
                    "script_code": script.get("code") or None,
                    "status": STATUS_COMPLETED,
                    "completed_at": _now(),
                },
            )
            if updated:
                logger.info(
                    "库外诉求回填完成 request=%s -> script=%s", r["id"], script.get("id")
                )

    # ================= 出参组装 =================
    async def _to_items(self, rows: List[Dict[str, Any]]) -> List[ScriptRequestItem]:
        script_ids = [str(r["script_id"]) for r in rows if r.get("script_id")]
        script_map = {str(s["id"]): s for s in await self.scripts.get_scripts(script_ids)}
        indexed_ids: set = set()
        try:
            indexed_ids = await self.repo.list_indexed_script_ids()
        except DatabaseError as exc:  # noqa: BLE001
            logger.warning("读取已解析剧本失败，列表 has_guide 标记可能不准确: %s", exc)
        return [self._to_item(row, script_map=script_map, indexed_ids=indexed_ids) for row in rows]

    def _to_item(
        self,
        row: Dict[str, Any],
        *,
        script_map: Optional[Dict[str, Dict[str, Any]]] = None,
        indexed_ids: Optional[set] = None,
        already_exists: bool = False,
    ) -> ScriptRequestItem:
        script_map = script_map or {}
        indexed_ids = indexed_ids or set()
        script_id = str(row["script_id"]) if row.get("script_id") else None
        script = script_map.get(script_id) if script_id else None
        return ScriptRequestItem(
            id=str(row["id"]),
            script_id=script_id,
            script_code=row.get("script_code") or (script.get("code") if script else None),
            script_title=row.get("script_title") or "",
            reason=row.get("reason"),
            cover_url=script.get("cover_url") if script else None,
            has_guide=bool(script_id and script_id in indexed_ids),
            status=row.get("status") or STATUS_PENDING,
            cancelled_at=row.get("cancelled_at"),
            completed_at=row.get("completed_at"),
            created_at=row.get("created_at"),
            already_exists=already_exists,
        )


_service: Optional[ScriptRequestService] = None


def get_script_request_service() -> ScriptRequestService:
    global _service
    if _service is None:
        _service = ScriptRequestService()
    return _service
