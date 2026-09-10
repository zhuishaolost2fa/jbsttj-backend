"""站内消息（收件箱）接口。

语义：
- 消息只能由后端在事件发生时投递（问题被回答 / 求的剧本已解析），
  没有「发消息」接口 —— 这是收件箱，不是聊天；
- 所有读写都限定在当前登录用户，跨用户访问一律 404（不暴露存在性）；
- 已读是软状态（read_at），未读数实时从库里数（走部分索引）。

注册顺序：静态路径（unread-count / read-all）必须排在 `/{message_id}` 之前。
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Body, Depends, Path, Query, status

from app.core.security import CurrentUser, get_current_user
from app.schemas.message import (
    MarkReadResult,
    MessageListResult,
    UnreadCountResult,
)
from app.services.message_service import MessageService, get_message_service

router = APIRouter(prefix="/messages", tags=["消息"])


@router.get(
    "/unread-count",
    response_model=UnreadCountResult,
    response_model_by_alias=True,
    summary="未读消息数",
    description="当前用户的未读消息条数，供 tabbar 红点轻量轮询（建议 30s 以上一次）。",
)
async def unread_count(
    user: CurrentUser = Depends(get_current_user),
    service: MessageService = Depends(get_message_service),
) -> UnreadCountResult:
    return await service.unread_count(user.id)


@router.get(
    "",
    response_model=MessageListResult,
    response_model_by_alias=True,
    summary="我的消息列表",
    description=(
        "当前用户的站内消息，按时间倒序。\n\n"
        "- `type` 过滤：`system` 系统通知 / `question_answered` 问题被回答 / "
        "`script_parsed` 求的剧本已解析；\n"
        "- `unreadOnly=true` 只看未读；\n"
        "- `unreadCount` 是**全量**未读数，不受本次分页与类型过滤影响；\n"
        "- 每条的 `data` 带事件上下文（scriptId / scriptCode / scriptTitle / "
        "questionId / requestId），点消息可直接跳详情页。"
    ),
)
async def list_messages(
    msg_type: Optional[str] = Query(
        default=None,
        alias="type",
        description="按类型过滤：system / question_answered / script_parsed",
    ),
    unread_only: bool = Query(default=False, alias="unreadOnly", description="只看未读"),
    limit: int = Query(default=20, ge=1, le=100, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    user: CurrentUser = Depends(get_current_user),
    service: MessageService = Depends(get_message_service),
) -> MessageListResult:
    return await service.list_messages(
        user.id,
        msg_type=msg_type,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/read-all",
    response_model=MarkReadResult,
    response_model_by_alias=True,
    summary="全部标记已读",
    description="把当前用户的全部未读消息置为已读，返回实际变更条数与剩余未读数。",
)
async def read_all(
    user: CurrentUser = Depends(get_current_user),
    service: MessageService = Depends(get_message_service),
) -> MarkReadResult:
    return await service.mark_read(user.id)


@router.patch(
    "/{message_id}/read",
    response_model=MarkReadResult,
    response_model_by_alias=True,
    summary="标记单条已读",
    description=(
        "把一条消息置为已读。只能操作自己的消息，否则 404。\n"
        "重复调用幂等（`updated=0`），返回里带上操作后的剩余未读数。"
    ),
)
async def mark_read(
    message_id: str = Path(description="消息 ID"),
    user: CurrentUser = Depends(get_current_user),
    service: MessageService = Depends(get_message_service),
) -> MarkReadResult:
    return await service.mark_read(user.id, [message_id])


@router.post(
    "/read-batch",
    response_model=MarkReadResult,
    response_model_by_alias=True,
    summary="批量标记已读",
    description="一次把多条消息置为已读，body 传消息 ID 数组（最多 100 条）。",
)
async def mark_read_batch(
    message_ids: List[str] = Body(default_factory=list, embed=True, description="消息 ID 列表"),
    user: CurrentUser = Depends(get_current_user),
    service: MessageService = Depends(get_message_service),
) -> MarkReadResult:
    return await service.mark_read(user.id, message_ids[:100])


@router.delete(
    "/{message_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除消息",
    description="删除自己的一条消息（物理删除）。只能删自己的，否则 404。",
)
async def delete_message(
    message_id: str = Path(description="消息 ID"),
    user: CurrentUser = Depends(get_current_user),
    service: MessageService = Depends(get_message_service),
) -> None:
    await service.delete(user.id, message_id)
