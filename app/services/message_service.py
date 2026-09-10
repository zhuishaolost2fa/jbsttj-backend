"""站内消息（收件箱）：读取、已读标记，以及事件发生时的投递。

分两条写入路径，但**幂等规则只有一份**（数据库 RPC `push_user_message`）：

- ``MessageService`` —— 异步，给 FastAPI 接口与异步业务（回答问题）用；
- ``SyncMessageClient`` —— 同步，给 Celery worker（`dm.finalize`）用。
  worker 里起 event loop 太重（httpx 连接池绑定 loop，每次新建代价高），
  所以这里跟 `dm_store.DMStore` 一样直接持有一个 `httpx.Client`。

**消息是旁路能力**：任何投递失败都只记日志，绝不能把主流程（回答问题、
解析完成翻状态）拖成失败。调用方可以完全不关心投递结果。

新增消息类型的完整清单：
1. `sql/user_messages.sql` 的 `ck_user_messages_type` 约束；
2. `app/schemas/message.py` 的 `MessageType`；
3. 本模块的文案构造函数（``_compose_*``）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import DatabaseError
from app.schemas.common import Pagination
from app.schemas.message import (
    MarkReadResult,
    MessageItem,
    MessageListResult,
    MessageType,
    UnreadCountResult,
)
from app.services.repository import MessageRepository, normalize_title_key

logger = logging.getLogger("app.messages")

# 正文里的问题 / 答案摘要长度：消息卡片只放得下一两行，全文去详情页看
_SNIPPET = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snippet(text: str, limit: int = _SNIPPET) -> str:
    """截一段纯文本摘要（换行折叠成空格，避免撑破卡片布局）。"""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else f"{flat[:limit]}…"


def _as_utc(value: Any) -> Optional[datetime]:
    """PostgREST 返回的是 ISO 字符串；pydantic 其实能直接吃，这里只做兜底。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ============================================================
# 文案构造：消息类型 → 标题 / 正文 / 上下文
# ============================================================
def _compose_question_answered(
    *, question: str, answer: str, script_title: str = ""
) -> tuple[str, str]:
    title = "你的问题有答案了"
    if script_title:
        title = f"《{script_title}》的问题有答案了"
    content = f"问：{_snippet(question)}\n答：{_snippet(answer)}"
    return title, content


def _compose_script_parsed(*, script_title: str) -> tuple[str, str]:
    title = f"你求的《{script_title}》已解析完成" if script_title else "你求的剧本已解析完成"
    content = (
        f"《{script_title}》的 DM 主持人手册已上线，可以去查看完整解析了。"
        if script_title
        else "有剧本的 DM 主持人手册已上线，可以去查看完整解析了。"
    )
    return title, content


# ============================================================
# 异步 Service（FastAPI 侧）
# ============================================================
class MessageService:
    """消息收件箱的读与已读标记。

    只服务「我的消息」：所有查询都强制带 user_id，绝不提供跨用户读法。
    """

    def __init__(self, repo: Optional[MessageRepository] = None) -> None:
        self.repo = repo or MessageRepository()

    # ---------------- 读 ----------------
    async def list_messages(
        self,
        user_id: str,
        *,
        msg_type: Optional[str] = None,
        unread_only: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> MessageListResult:
        if msg_type and msg_type not in MessageType.ALL:
            msg_type = None  # 未知类型按「全部」处理，不报错：消息是旁路内容
        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        rows, total = await self.repo.list_for_user(
            user_id, msg_type=msg_type, unread_only=unread_only, limit=limit, offset=offset
        )
        # 未读数取全量（不受类型 / 分页影响），前端红点要的是总数
        unread = await self.repo.count_unread(user_id)
        return MessageListResult(
            items=[_to_item(r) for r in rows],
            pagination=Pagination(
                total=total,
                limit=limit,
                offset=offset,
                has_more=offset + len(rows) < total,
            ),
            unread_count=unread,
        )

    async def unread_count(self, user_id: str) -> UnreadCountResult:
        return UnreadCountResult(unread_count=await self.repo.count_unread(user_id))

    # ---------------- 写（已读 / 删除）----------------
    async def mark_read(
        self, user_id: str, message_ids: Optional[Sequence[str]] = None
    ) -> MarkReadResult:
        ids = [str(i) for i in dict.fromkeys(message_ids or []) if i]
        updated = await self.repo.mark_read(user_id, ids or None)
        return MarkReadResult(
            updated=updated, unread_count=await self.repo.count_unread(user_id)
        )

    async def delete(self, user_id: str, message_id: str) -> None:
        await self.repo.delete(user_id, message_id)

    # ---------------- 投递（异步业务侧）----------------
    async def notify_question_answered(
        self,
        *,
        question_id: str,
        asker_id: str,
        answered_by: str,
        question: str,
        answer: str,
        script_id: str = "",
        script_code: str = "",
        script_title: str = "",
    ) -> Optional[str]:
        """问题被真人解答 → 通知提问者。

        **自己回答自己的问题不发**（自问自答没有通知价值）。
        """
        if not asker_id or asker_id == answered_by:
            return None
        title, content = _compose_question_answered(
            question=question, answer=answer, script_title=script_title
        )
        try:
            return await self.repo.push(
                user_id=asker_id,
                msg_type=MessageType.QUESTION_ANSWERED,
                title=title,
                content=content,
                actor_id=answered_by,
                data={
                    "scriptId": script_id or None,
                    "scriptCode": script_code or None,
                    "scriptTitle": script_title or None,
                    "questionId": question_id,
                },
                dedup_key=f"question_answered:{question_id}",
            )
        except DatabaseError as exc:
            logger.warning("投递「问题被回答」消息失败 question=%s: %s", question_id, exc)
            return None

    async def notify_script_parsed(
        self,
        *,
        request_id: str,
        user_id: str,
        script_id: str = "",
        script_code: str = "",
        script_title: str = "",
    ) -> Optional[str]:
        """求的剧本已解析完成 → 通知求解析的人。"""
        if not user_id:
            return None
        title, content = _compose_script_parsed(script_title=script_title)
        try:
            return await self.repo.push(
                user_id=user_id,
                msg_type=MessageType.SCRIPT_PARSED,
                title=title,
                content=content,
                data={
                    "scriptId": script_id or None,
                    "scriptCode": script_code or None,
                    "scriptTitle": script_title or None,
                    "requestId": request_id,
                },
                dedup_key=f"script_parsed:{request_id}",
            )
        except DatabaseError as exc:
            logger.warning("投递「剧本已解析」消息失败 request=%s: %s", request_id, exc)
            return None


def _to_item(row: Dict[str, Any]) -> MessageItem:
    data = row.get("data")
    return MessageItem(
        id=str(row.get("id") or ""),
        type=row.get("type") or MessageType.SYSTEM,
        title=row.get("title") or "",
        content=row.get("content") or "",
        actor_id=str(row["actor_id"]) if row.get("actor_id") else None,
        data=data if isinstance(data, dict) else {},
        read=bool(row.get("read_at")),
        read_at=_as_utc(row.get("read_at")),
        created_at=_as_utc(row.get("created_at")),
    )


_service: Optional[MessageService] = None


def get_message_service() -> MessageService:
    global _service
    if _service is None:
        _service = MessageService()
    return _service


# ============================================================
# 同步写入器（Celery worker 侧）
# ============================================================
class SyncMessageClient:
    """worker 用的同步消息写入器：直连 PostgREST，不开 event loop。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._client: Optional[httpx.Client] = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            s = self._settings
            if not s.supabase_url or not s.supabase_service_role_key:
                raise DatabaseError("Supabase 未配置，无法投递站内消息")
            self._client = httpx.Client(
                base_url=s.supabase_rest_url,
                headers={
                    "apikey": s.supabase_service_role_key,
                    "Authorization": f"Bearer {s.supabase_service_role_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(15.0, connect=10.0),
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def rpc(self, name: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            resp = self.client.post(f"/rpc/{name}", json=params or {})
        except httpx.HTTPError as exc:
            raise DatabaseError(f"RPC {name} 请求失败: {exc}") from exc
        if resp.status_code >= 400:
            detail: Any
            try:
                detail = resp.json()
            except Exception:  # noqa: BLE001
                detail = resp.text[:300]
            logger.error("消息 RPC %s -> %s %s", name, resp.status_code, detail)
            raise DatabaseError(f"RPC {name} 失败", details=detail)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def push(
        self,
        *,
        user_id: str,
        msg_type: str,
        title: str,
        content: str = "",
        actor_id: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        dedup_key: Optional[str] = None,
    ) -> Optional[str]:
        result = self.rpc(
            "push_user_message",
            {
                "p_user_id": str(user_id),
                "p_type": msg_type,
                "p_title": title,
                "p_content": content or "",
                "p_actor_id": str(actor_id) if actor_id else None,
                "p_data": data or {},
                "p_dedup_key": dedup_key,
            },
        )
        return str(result) if result else None

    def settle_script_requests(
        self,
        *,
        script_id: str = "",
        script_code: str = "",
        script_title: str = "",
    ) -> List[Dict[str, Any]]:
        """剧本解析完成 → 结算待处理诉求，返回被置 completed 的行。

        ``script_title`` 用来匹配「库外诉求」（只填了标题、没有 script_id 的
        求解析）：按归一化标题键比对，与后端惰性同步同一套规则。
        """
        keys: List[str] = []
        if script_title:
            key = normalize_title_key(script_title)
            if key:
                keys.append(key)
        rows = self.rpc(
            "settle_script_requests",
            {
                "p_script_id": str(script_id) if script_id else None,
                "p_script_code": script_code or None,
                "p_match_keys": keys,
            },
        )
        return rows if isinstance(rows, list) else []


def notify_script_parsed_sync(
    *,
    script_id: str = "",
    script_code: str = "",
    script_title: str = "",
) -> int:
    """worker 侧入口：剧本解析完成 → 结算诉求 + 逐个投递消息。

    返回实际投递成功的条数。**全程吞异常**：消息是锦上添花，
    绝不能让「发通知失败」把已经解析成功的流水线拖成失败。
    """
    client = SyncMessageClient()
    try:
        try:
            rows = client.settle_script_requests(
                script_id=script_id, script_code=script_code, script_title=script_title
            )
        except DatabaseError as exc:
            logger.warning("结算求解析诉求失败 script=%s: %s", script_id or script_code, exc)
            return 0

        sent = 0
        for row in rows:
            request_id = str(row.get("id") or "")
            user_id = str(row.get("user_id") or "")
            if not user_id:
                continue
            title, content = _compose_script_parsed(
                script_title=script_title or (row.get("script_title") or "")
            )
            try:
                mid = client.push(
                    user_id=user_id,
                    msg_type=MessageType.SCRIPT_PARSED,
                    title=title,
                    content=content,
                    data={
                        "scriptId": str(row.get("script_id") or script_id) or None,
                        "scriptCode": row.get("script_code") or script_code or None,
                        "scriptTitle": row.get("script_title") or script_title or None,
                        "requestId": request_id,
                    },
                    dedup_key=f"script_parsed:{request_id}",
                )
                if mid:
                    sent += 1
            except DatabaseError as exc:
                logger.warning(
                    "投递「剧本已解析」消息失败 request=%s: %s", request_id, exc
                )

        if rows:
            logger.info(
                "剧本解析完成 script=%s：结算 %s 条求解析，投递 %s 条消息",
                script_code or script_id, len(rows), sent,
            )
        return sent
    finally:
        client.close()
