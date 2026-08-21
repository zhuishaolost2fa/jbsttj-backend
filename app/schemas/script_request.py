"""剧本「求解析」的请求与响应模型。

模块内全部出参统一小驼峰（camelCase），与 H5 前端直接消费的
`/byname`、`/import-status`、`/dm-guide/*` 保持同款命名习惯，
前端无需再做下划线 → 驼峰映射。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.schemas.common import Pagination

# 状态机与 sql/script_requests.sql 的 CHECK 约束保持一致
SCRIPT_REQUEST_STATUSES = ("pending", "completed", "cancelled")


class ScriptRequestCreate(BaseModel):
    """发起一次求解析。

    目标剧本三选一，按优先级解析：
    - 传 `scriptId` 或 `scriptCode`：定位剧本库中的剧本（必须存在）；
    - 只传 `scriptTitle`：先在剧本库按归一化标题匹配，命中则关联该剧本，
      未命中则作为「库外剧本」的求解析诉求（仅保留标题文本）。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    script_id: Optional[str] = Field(
        default=None, description="剧本 UUID，与 scriptCode 二选一"
    )
    script_code: Optional[str] = Field(
        default=None, description="剧本业务编码，与 scriptId 二选一"
    )
    script_title: str = Field(
        max_length=200, description="剧本名称（必填）。库中剧本会自动回填真实标题"
    )
    reason: Optional[str] = Field(
        default=None, max_length=500, description="期望解析的原因 / 补充说明"
    )

    @field_validator("script_title", "reason")
    @classmethod
    def _strip_text(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        return v or None

    @field_validator("script_title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        if not v:
            raise ValueError("剧本名称不能为空")
        return v


class ScriptRequestItem(BaseModel):
    """一条求解析记录（我的列表 / 创建 / 取消的返回）。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str = Field(description="求解析记录 ID")
    script_id: Optional[str] = Field(default=None, description="关联的剧本 ID（库外剧本为空）")
    script_code: Optional[str] = Field(default=None, description="关联的剧本业务编码")
    script_title: str = Field(description="剧本名称")
    reason: Optional[str] = Field(default=None, description="期望解析的原因")
    cover_url: Optional[str] = Field(default=None, description="剧本封面（库中剧本才有）")
    # 该剧本当前是否已解析完成（读取时与 script_dm_documents 对照实时判定）
    has_guide: bool = Field(
        default=False, description="剧本是否已解析完成（有可检索的 DM 手册）"
    )
    status: str = Field(description="状态：pending 待解析 / completed 已完成 / cancelled 已取消")
    cancelled_at: Optional[str] = Field(default=None, description="取消时间")
    completed_at: Optional[str] = Field(default=None, description="完成时间")
    created_at: Optional[str] = Field(default=None, description="发起时间")
    # 仅创建接口返回：true 表示命中了已有的求解析记录（重复求 / 取消后复活），未新建
    already_exists: bool = Field(
        default=False, description="true=复用已有求解析记录（重复发起或取消后复活）"
    )


class ScriptRequestListResult(BaseModel):
    """我的求解析列表（分页）。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[ScriptRequestItem] = Field(default_factory=list, description="求解析列表")
    pagination: Pagination = Field(description="分页信息")


class ScriptRequestLeaderboardItem(BaseModel):
    """排行榜上一部剧的诉求聚合。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    script_id: Optional[str] = Field(default=None, description="剧本 ID（库外剧本为空）")
    script_code: Optional[str] = Field(default=None, description="剧本业务编码")
    script_title: str = Field(description="剧本名称")
    cover_url: Optional[str] = Field(default=None, description="剧本封面")
    request_count: int = Field(description="希望解析该剧本的用户数（去重后）")


class ScriptRequestLeaderboardResult(BaseModel):
    """求解析排行榜。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[ScriptRequestLeaderboardItem] = Field(
        default_factory=list, description="按诉求人数降序的榜单"
    )
    pagination: Pagination = Field(description="分页信息")
