"""剧本库的请求与响应模型。

三个模型的分工：
  - :class:`ScriptCreate` —— 新增，title 必填，其余可选；
  - :class:`ScriptUpdate` —— 修改，**全部字段可选**，只提交要改的字段（PATCH 语义）；
  - :class:`ScriptItem`   —— 出参，把库里的行翻译成前端结构。

字典编码（玩法/题材/发行方式/难度）在这一层只做格式校验，
「编码是否真实存在」交给 service 层比对字典，那里能给出更友好的报错。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.schemas.common import Pagination

# 剧本 code 规范：小写字母/数字/连字符，与 sql/scripts.sql 的 CHECK 约束保持一致
CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")

SCRIPT_STATUSES = ("published", "draft", "offline")


def _clean_list(values: Optional[List[str]]) -> Optional[List[str]]:
    """去空白、去空串、去重且保持原顺序。"""
    if values is None:
        return None
    seen: Dict[str, None] = {}
    for v in values:
        v = (v or "").strip()
        if v:
            seen.setdefault(v, None)
    return list(seen)


class ScriptBase(BaseModel):
    """新增与修改共用的字段定义。"""

    title: Optional[str] = Field(default=None, max_length=200, description="剧本名称")
    code: Optional[str] = Field(
        default=None,
        max_length=64,
        description="业务编码（小写字母/数字/连字符），不传则由后端按标题自动生成",
    )
    aliases: Optional[List[str]] = Field(default=None, description="别名/副标题，参与搜索匹配")
    summary: Optional[str] = Field(default=None, max_length=2000, description="剧本简介")
    author: Optional[str] = Field(default=None, max_length=200, description="作者")
    publisher: Optional[str] = Field(default=None, max_length=200, description="发行方")

    release_type: Optional[str] = Field(
        default=None, max_length=64, description="发行方式编码，取自字典维度 release"
    )
    difficulty: Optional[str] = Field(
        default=None, max_length=64, description="难度编码，取自字典维度 difficulty"
    )
    playstyles: Optional[List[str]] = Field(
        default=None, description="玩法编码列表，取自字典维度 playstyle"
    )
    themes: Optional[List[str]] = Field(
        default=None, description="题材编码列表，取自字典维度 theme"
    )
    tags: Optional[List[str]] = Field(default=None, description="自由标签，不受字典约束")

    player_min: Optional[int] = Field(default=None, ge=1, le=50, description="最少人数")
    player_max: Optional[int] = Field(default=None, ge=1, le=50, description="最多人数")
    male_count: Optional[int] = Field(default=None, ge=0, le=50, description="男性角色数")
    female_count: Optional[int] = Field(default=None, ge=0, le=50, description="女性角色数")
    flexible_count: Optional[int] = Field(default=None, ge=0, le=50, description="不限性别角色数")
    allow_gender_swap: Optional[bool] = Field(default=None, description="是否可反串")

    duration_min: Optional[int] = Field(default=None, ge=0, le=2880, description="最短时长（分钟）")
    duration_max: Optional[int] = Field(default=None, ge=0, le=2880, description="最长时长（分钟）")

    rating: Optional[float] = Field(default=None, ge=0, le=10, description="评分，0~10")
    rating_count: Optional[int] = Field(default=None, ge=0, description="评分人数")
    play_count: Optional[int] = Field(default=None, ge=0, description="玩过人数，用于热度排序")
    published_year: Optional[int] = Field(default=None, ge=2010, le=2100, description="发行年份")
    cover_url: Optional[str] = Field(default=None, max_length=1000, description="封面图地址")
    is_recommended: Optional[bool] = Field(default=None, description="是否加入推荐位")
    status: Optional[str] = Field(default=None, description="状态：published / draft / offline")
    source: Optional[str] = Field(default=None, max_length=500, description="数据来源说明")
    extra: Optional[Dict[str, Any]] = Field(default=None, description="扩展字段，透传存储")

    @field_validator("code")
    @classmethod
    def _check_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().lower()
        if not CODE_PATTERN.match(v):
            raise ValueError("code 只能包含小写字母、数字与连字符，长度 2~64")
        return v

    @field_validator("title", "author", "publisher", "summary", "source", "cover_url")
    @classmethod
    def _strip_text(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        return v or None

    @field_validator("aliases", "playstyles", "themes", "tags")
    @classmethod
    def _normalize_list(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return _clean_list(v)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().lower()
        if v not in SCRIPT_STATUSES:
            raise ValueError(f"status 只能是 {' / '.join(SCRIPT_STATUSES)}")
        return v

    @model_validator(mode="after")
    def _check_ranges(self) -> "ScriptBase":
        """区间字段必须成对出现且方向正确。

        单独提交一侧会让数据库里出现「有下限没上限」的半截区间，
        范围查询会直接漏掉这条记录，所以在入口就拦掉。
        """
        if (self.player_min is None) != (self.player_max is None):
            raise ValueError("player_min 与 player_max 必须同时提供")
        if (
            self.player_min is not None
            and self.player_max is not None
            and self.player_min > self.player_max
        ):
            raise ValueError("player_min 不能大于 player_max")

        if (self.duration_min is None) != (self.duration_max is None):
            raise ValueError("duration_min 与 duration_max 必须同时提供")
        if (
            self.duration_min is not None
            and self.duration_max is not None
            and self.duration_min > self.duration_max
        ):
            raise ValueError("duration_min 不能大于 duration_max")
        return self


class ScriptCreate(ScriptBase):
    """新增剧本入参：只有 title 必填。"""

    title: str = Field(max_length=200, description="剧本名称（必填）")

    model_config = {
        "json_schema_extra": {
            "example": {
                "title": "雾都疑影",
                "summary": "一桩发生在雾都旅馆的连环失踪案。",
                "author": "张三",
                "publisher": "示例工作室",
                "release_type": "boxed",
                "difficulty": "intermediate",
                "playstyles": ["hardcore", "honkaku"],
                "themes": ["western", "suspense"],
                "tags": ["暴风雪山庄"],
                "player_min": 6,
                "player_max": 6,
                "male_count": 3,
                "female_count": 3,
                "duration_min": 240,
                "duration_max": 300,
                "published_year": 2024,
            }
        }
    }


class ScriptUpdate(ScriptBase):
    """修改剧本入参：所有字段可选，只提交需要变更的字段。

    未出现在请求体中的字段保持原值；显式传 `null` 表示把该字段清空。
    """

    model_config = {
        "json_schema_extra": {
            "example": {
                "difficulty": "advanced",
                "tags": ["暴风雪山庄", "多重反转"],
                "rating": 8.7,
                "is_recommended": True,
            }
        }
    }


class LabeledCode(BaseModel):
    """编码 + 中文标签，省得前端再拿字典做一次映射。"""

    code: str = Field(description="字典编码")
    label: str = Field(description="中文标签")


class ScriptItem(BaseModel):
    """剧本出参。"""

    id: str = Field(description="剧本 ID")
    code: str = Field(description="业务编码")
    title: str = Field(description="剧本名称")
    aliases: List[str] = Field(default_factory=list, description="别名")
    summary: Optional[str] = Field(default=None, description="简介")
    author: Optional[str] = Field(default=None, description="作者")
    publisher: Optional[str] = Field(default=None, description="发行方")

    release_type: Optional[str] = Field(default=None, description="发行方式编码")
    difficulty: Optional[str] = Field(default=None, description="难度编码")
    playstyles: List[str] = Field(default_factory=list, description="玩法编码")
    themes: List[str] = Field(default_factory=list, description="题材编码")
    tags: List[str] = Field(default_factory=list, description="自由标签")

    # 标签化结果：后端已经查过字典，前端直接展示即可
    release_label: Optional[str] = Field(default=None, description="发行方式中文标签")
    difficulty_label: Optional[str] = Field(default=None, description="难度中文标签")
    playstyle_labels: List[LabeledCode] = Field(default_factory=list, description="玩法标签")
    theme_labels: List[LabeledCode] = Field(default_factory=list, description="题材标签")

    player_min: Optional[int] = Field(default=None, description="最少人数")
    player_max: Optional[int] = Field(default=None, description="最多人数")
    male_count: Optional[int] = Field(default=None, description="男性角色数")
    female_count: Optional[int] = Field(default=None, description="女性角色数")
    flexible_count: int = Field(default=0, description="不限性别角色数")
    allow_gender_swap: Optional[bool] = Field(default=None, description="是否可反串")
    player_text: Optional[str] = Field(default=None, description="人数展示文案，如「6人（3男3女）」")

    duration_min: Optional[int] = Field(default=None, description="最短时长（分钟）")
    duration_max: Optional[int] = Field(default=None, description="最长时长（分钟）")
    duration_text: Optional[str] = Field(default=None, description="时长展示文案，如「4-5小时」")

    rating: Optional[float] = Field(default=None, description="评分")
    rating_count: int = Field(default=0, description="评分人数")
    play_count: int = Field(default=0, description="玩过人数")
    published_year: Optional[int] = Field(default=None, description="发行年份")
    cover_url: Optional[str] = Field(default=None, description="封面图")
    is_recommended: bool = Field(default=False, description="是否推荐位")
    status: str = Field(default="published", description="状态")
    source: Optional[str] = Field(default=None, description="数据来源说明")
    extra: Dict[str, Any] = Field(default_factory=dict, description="扩展字段")
    # 是否挂了 DM 主持人手册（extra.dmGuide.objectKey 存在即视为已上传手册），
    # 供 H5「我的导入」列表直接判断该剧本是否进入了解析流程，免去逐条再查进度接口
    has_guide: bool = Field(default=False, description="是否已关联 DM 主持人手册（PDF/Word）")
    # 导入者（创建人）用户 ID：手册是 TA 上传的，问答页据此展示「感谢 xx 导入手册」。
    # 只透出 ID 本身，昵称/头像等展示信息由 DM 手册状态接口关联 profiles 后下发。
    created_by: Optional[str] = Field(default=None, description="导入者用户 ID（上传 DM 手册的人）")

    created_at: Optional[str] = Field(default=None, description="创建时间")
    updated_at: Optional[str] = Field(default=None, description="更新时间")


class ScriptListResult(BaseModel):
    """剧本列表分页结果。"""

    items: List[ScriptItem] = Field(description="剧本列表")
    pagination: Pagination = Field(description="分页信息")


class ScriptItemCamel(ScriptItem):
    """剧本出参的**小驼峰**版本，专供前端直接消费。

    字段含义与 :class:`ScriptItem` 完全一致，仅在序列化时把下划线命名翻成 JS 习惯的
    小驼峰：`player_min` → `playerMin`、`release_type` → `releaseType`、
    `is_recommended` → `isRecommended`、`created_at` → `createdAt`……

    Python 侧仍用 `player_min` 这样的蛇形名读写（已开 `populate_by_name`），不受影响，
    避免业务代码里到处写驼峰。
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class ScriptSearchByNameResult(BaseModel):
    """按名称查找剧本的返回结构。

    统一用 **HTTP 200** 返回：找到时 `found=true` 且 `items` 非空；找不到时
    `found=false`、`items=[]`，前端不必区分 200/404，直接看 `found` 即可。

    所有字段（含 `items` 内每个剧本）均为**小驼峰**，前端可直接消费。
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    found: bool = Field(description="是否找到匹配剧本")
    query: str = Field(description="回显本次查询名称")
    count: int = Field(description="匹配到的剧本数量")
    items: List[ScriptItemCamel] = Field(
        default_factory=list, description="匹配到的剧本列表（已按匹配质量排序）"
    )


class ScriptAutocompleteItem(BaseModel):
    """自动补全（联想）用的轻量剧本项。

    只带下拉框要展示的字段，不拉字典标签、不拼展示文案，避免联想接口被列表级的
    计算拖慢。和 `ScriptItem` 一样是蛇形命名，与剧本模块其余读接口保持一致。
    """

    id: str = Field(description="剧本 ID")
    code: str = Field(description="业务编码")
    title: str = Field(description="剧本名称")
    author: Optional[str] = Field(default=None, description="作者")
    cover_url: Optional[str] = Field(default=None, description="封面图")
    has_guide: bool = Field(
        default=False,
        description="是否已关联 DM 主持人手册，便于导入时提示「该剧本已导入过」",
    )


class ScriptAutocompleteResult(BaseModel):
    """自动补全（联想）结果。"""

    query: str = Field(description="回显本次输入片段")
    count: int = Field(description="匹配到的剧本数量")
    items: List[ScriptAutocompleteItem] = Field(
        default_factory=list, description="联想候选剧本列表"
    )


class ScriptCreateResult(ScriptItem):
    """新增 / 导入剧本的返回结构。

    在 :class:`ScriptItem` 基础上加一个 `was_created` 标识：

    - `true`  —— 剧本库原本没有该剧本，本次新建了一行；
    - `false` —— 剧本库已存在同名剧本，本次只是把 DM 手册**关联**到了它（去重），
      前端据此提示「已关联到已有剧本《xxx》」而不是「新建成功」，避免误以为重复导入。
    """

    was_created: bool = Field(
        default=True,
        description="true=本次新建剧本；false=关联到已有剧本（去重，未新建）",
    )
