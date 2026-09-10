"""用户消息（站内信）的出参结构。

设计要点：

1. **消息是只读收件箱** —— 用户只能读、标记已读、删除自己的消息，
   写入完全由后端在事件发生时触发（回答问题 / 剧本解析完成）；
2. **data 是事件上下文**（剧本 id / code / 标题、问题 id、求解析 id），
   前端点开消息直接跳详情页，不必再拿 id 反查一遍；
3. 时间字段统一 UTC ISO，前端本地化展示。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.schemas.common import Pagination


class MessageType:
    """消息类型常量。

    与 SQL 里 ``ck_user_messages_type`` 的取值严格对应，
    新增类型必须**同时**改三处：这里、SQL 约束、``app/services/message_service`` 的文案构造。
    """

    SYSTEM = "system"
    QUESTION_ANSWERED = "question_answered"
    SCRIPT_PARSED = "script_parsed"

    ALL = (SYSTEM, QUESTION_ANSWERED, SCRIPT_PARSED)

    # 前端需要「全部 / 已读 / 未读」之外的分类筛选时，取这里做下拉项
    LABELS = {
        SYSTEM: "系统通知",
        QUESTION_ANSWERED: "问题被回答",
        SCRIPT_PARSED: "求解析已达成",
    }


class MessageItem(BaseModel):
    """一条站内消息。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str = Field(description="消息 ID")
    type: str = Field(description="消息类型：system / question_answered / script_parsed")
    title: str = Field(description="消息标题")
    content: str = Field(default="", description="消息正文（摘要）")
    actor_id: Optional[str] = Field(default=None, description="触发者用户 ID（系统消息为空）")
    data: Dict[str, Any] = Field(
        default_factory=dict,
        description="事件上下文：scriptId / scriptCode / scriptTitle / questionId / requestId 等",
    )
    read: bool = Field(default=False, description="是否已读（read_at 非空即为已读）")
    read_at: Optional[datetime] = Field(default=None, description="已读时间")
    created_at: Optional[datetime] = Field(default=None, description="创建时间")


class MessageListResult(BaseModel):
    """消息列表。``unread_count`` 是**全量**未读数，不受本页筛选影响。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: List[MessageItem] = Field(default_factory=list, description="当前页消息")
    pagination: Pagination
    unread_count: int = Field(default=0, description="全部未读条数（不受分页 / 类型筛选影响）")


class UnreadCountResult(BaseModel):
    """未读数量，供 tabbar 红点轮询。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    unread_count: int = Field(default=0, description="未读条数")


class MarkReadResult(BaseModel):
    """标记已读的结果。``updated`` 为实际从未读翻成已读的条数。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    updated: int = Field(default=0, description="本次标记为已读的条数")
    unread_count: int = Field(default=0, description="操作后剩余未读数")
