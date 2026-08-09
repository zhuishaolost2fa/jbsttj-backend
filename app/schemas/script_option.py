"""剧本杀筛选维度与选项的响应模型。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ScriptOptionItem(BaseModel):
    """单个筛选选项。

    `min_value` / `max_value` 仅「人数」「时长」两个维度有值，
    前端可直接把它们回传给列表接口做范围过滤，无需在前端硬编码数字。
    """

    code: str = Field(description="选项编码，筛选时回传该值")
    label: str = Field(description="展示文案")
    aliases: List[str] = Field(default_factory=list, description="口语别名，可用于搜索")
    description: Optional[str] = Field(default=None, description="选项说明")
    min_value: Optional[int] = Field(default=None, description="区间下限（含），仅区间型维度有值")
    max_value: Optional[int] = Field(default=None, description="区间上限（含），仅区间型维度有值")
    unit: Optional[str] = Field(default=None, description="区间单位：person=人，minute=分钟")
    sort_order: int = Field(default=0, description="排序值，越小越靠前")
    is_hot: bool = Field(default=False, description="是否热门，前端可高亮或前置")

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "ScriptOptionItem":
        return cls(
            code=row.get("code") or "",
            label=row.get("label") or "",
            aliases=list(row.get("aliases") or []),
            description=row.get("description"),
            min_value=row.get("min_value"),
            max_value=row.get("max_value"),
            unit=row.get("unit"),
            sort_order=int(row.get("sort_order") or 0),
            is_hot=bool(row.get("is_hot")),
        )


class ScriptOptionCategory(BaseModel):
    """筛选维度（玩法 / 题材 / 发行方式 / 难度 / 人数 / 时长）。"""

    code: str = Field(description="维度编码，用作 /script-options/{category} 的路径参数")
    name: str = Field(description="维度名称")
    description: Optional[str] = Field(default=None, description="维度说明")
    multi_select: bool = Field(default=True, description="前端筛选器是否允许多选")
    sort_order: int = Field(default=0, description="排序值，越小越靠前")
    option_count: int = Field(default=0, description="该维度下启用中的选项数量")

    @classmethod
    def from_row(cls, row: Dict[str, Any], option_count: int = 0) -> "ScriptOptionCategory":
        return cls(
            code=row.get("code") or "",
            name=row.get("name") or "",
            description=row.get("description"),
            multi_select=bool(row.get("multi_select", True)),
            sort_order=int(row.get("sort_order") or 0),
            option_count=option_count,
        )


class ScriptOptionGroup(ScriptOptionCategory):
    """维度 + 其下全部选项，供前端一次性渲染筛选器。"""

    options: List[ScriptOptionItem] = Field(default_factory=list, description="该维度下的选项")


class ScriptOptionTree(BaseModel):
    """全量筛选器结构。"""

    categories: List[ScriptOptionGroup] = Field(description="按 sort_order 升序的维度列表")
    total_categories: int = Field(description="维度总数")
    total_options: int = Field(description="选项总数")


class ScriptOptionListResult(BaseModel):
    """单维度选项列表。"""

    category: ScriptOptionCategory = Field(description="所属维度信息")
    items: List[ScriptOptionItem] = Field(description="选项列表")
    total: int = Field(description="选项总数")
