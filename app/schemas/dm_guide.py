"""DM 主持人手册 RAG 相关的请求 / 响应模型。

这里顺带把 `scripts.extra->'dmGuide'` 这个**隐式契约显式化**。
在此之前，前端上传完 PDF 往 `extra` 里塞什么键、后端读哪几个键，
只散落在 SQL 注释和 Celery 任务的形参里，谁都能改、谁都不知道改了会炸。
`DMGuideRef` 把它固定成一个可校验的结构：键名写错会在触发入库时立刻报 422，
而不是等流水线跑到一半在 worker 日志里静默失败。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class DMGuideRef(BaseModel):
    """`scripts.extra.dmGuide` 的结构约定。

    前端走分片上传拿到 OSS 对象后，把这几个字段写进剧本的 `extra.dmGuide`，
    后端据此定位文件并启动解析。只有 `objectKey` 是硬性required —— 其余字段
    缺失只影响展示与体积预校验，不影响解析本身。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    object_key: str = Field(description="OSS 对象键，如 `scripts/xxx/dm-guide.pdf`")
    file_id: Optional[str] = Field(default=None, description="上传记录 ID，用于回溯来源")
    file_name: Optional[str] = Field(default=None, description="原始文件名")
    file_size: Optional[int] = Field(default=None, ge=0, description="文件字节数")

    @field_validator("object_key")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("objectKey 不能为空")
        return v

    @classmethod
    def from_extra(cls, extra: Optional[Dict[str, Any]]) -> Optional["DMGuideRef"]:
        """从剧本的 extra 字段里解出 dmGuide，解不出返回 None（不抛异常）。

        兼容三种历史写法：``{"dmGuide": {...}}``、``{"dm_guide": {...}}``，
        以及直接把 objectKey 写成字符串的 ``{"dmGuide": "scripts/x.pdf"}``。
        """
        if not extra:
            return None
        raw = extra.get("dmGuide") or extra.get("dm_guide")
        if not raw:
            return None
        if isinstance(raw, str):
            raw = {"objectKey": raw}
        if not isinstance(raw, dict):
            return None
        try:
            return cls.model_validate(raw)
        except Exception:  # noqa: BLE001 - extra 是自由字典，脏数据不该炸掉剧本接口
            return None


# ------------------------------------------------------------
# 入库任务
# ------------------------------------------------------------
class IngestRequest(BaseModel):
    """手动触发入库。

    正常链路是「保存剧本时检测到 dmGuide 自动触发」，这个接口用于两种情况：
    手册解析失败后重试，以及换了模型/参数需要重算。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    object_key: Optional[str] = Field(
        default=None,
        description="覆盖剧本 extra.dmGuide.objectKey，不传则用剧本上记录的那份",
    )
    force: bool = Field(
        default=False,
        description=(
            "true 时即便内容指纹命中已完成的旧版本也强制重跑。"
            "重跑会消耗完整的 embedding 与 LLM 额度，谨慎使用"
        ),
    )


class JobProgress(BaseModel):
    """任务进度快照。

    进度不用单一百分比表示 —— 四个阶段的耗时量级差着两个数量级
    （提取几十秒、QA 生成十几分钟），拿一个数字线性插值只会误导前端。
    这里把各阶段的原始计数原样透出，由前端决定怎么展示。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    job_id: str = Field(description="任务 ID")
    script_id: str = Field(description="剧本 ID")
    document_id: Optional[str] = Field(default=None, description="文档 ID，建档后才有")
    status: str = Field(
        description=(
            "pending / downloading / extracting / chunking / "
            "generating_qa / embedding / completed / failed / cancelled"
        )
    )
    stage_detail: Optional[str] = Field(default=None, description="当前阶段的人类可读描述")
    total_pages: int = Field(default=0, description="总页数")
    processed_pages: int = Field(default=0, description="已提取页数")
    total_shards: int = Field(default=0, description="分片总数")
    finished_shards: int = Field(default=0, description="已完成分片数")
    total_chunks: int = Field(default=0, description="切出的分块数")
    embedded_chunks: int = Field(default=0, description="已向量化并入库的分块数")
    total_qa: int = Field(default=0, description="生成的问答对数")
    embedded_qa: int = Field(default=0, description="已向量化并入库的问答对数")
    error_message: Optional[str] = Field(default=None, description="失败原因")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")

    @property
    def is_terminal(self) -> bool:
        return self.status in {"completed", "failed", "cancelled"}


class IngestResponse(BaseModel):
    """触发结果。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    job_id: str = Field(description="任务 ID，用轮询进度接口跟踪")
    status: str = Field(description="任务当前状态")
    reused: bool = Field(
        default=False,
        description="true 表示命中了正在跑的同剧本任务，未重复派发",
    )
    message: str = Field(default="", description="给调用方的提示")


# ------------------------------------------------------------
# 检索
# ------------------------------------------------------------
class ChunkHit(BaseModel):
    """命中的原文分块。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str = Field(description="分块 ID")
    document_id: str = Field(description="所属文档 ID")
    content: str = Field(description="分块原文")
    section_path: List[str] = Field(default_factory=list, description="章节面包屑")
    page_start: int = Field(default=0, description="起始页")
    page_end: int = Field(default=0, description="结束页")
    similarity: float = Field(default=0.0, description="余弦相似度，越大越相关")


class QAHit(BaseModel):
    """命中的问答对。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str = Field(description="问答对 ID")
    document_id: str = Field(description="所属文档 ID")
    question: str = Field(description="问题")
    answer: str = Field(description="答案")
    category: Optional[str] = Field(default=None, description="问题分类")
    chunk_id: Optional[str] = Field(default=None, description="来源分块 ID")
    similarity: float = Field(default=0.0, description="余弦相似度")


class RetrievedHit(BaseModel):
    """统一、按「qa 优先」排序后的召回条目，便于上层直接按序消费。

    `similarity` 是排序分（qa 已乘 `dm_search_qa_boost`），`raw_similarity`
    是原始余弦相似度——前端若想严格按真实相关度展示，用 `raw_similarity`。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str = Field(description="来源类型：qa / chunk")
    similarity: float = Field(default=0.0, description="排序分（qa 已加权）")
    raw_similarity: float = Field(default=0.0, description="原始余弦相似度")
    payload: Dict[str, Any] = Field(default_factory=dict, description="qa 或 chunk 的原始字段")


class SearchResult(BaseModel):
    """检索结果。

    `chunks` 与 `qa` 同时返回而不是二选一：问答对命中率高但覆盖面窄，
    原文块覆盖全但相关性判断更依赖上下文，两者互补。
    **hybrid 模式以 qa 为主召回**——qa 取满 top_k 做主答案来源，
    chunk 仅作出处佐证、配额收紧到 `dm_search_qa_supplement_k`。
    `hits` 是两者合并后、qa 优先的扁平视图，上层可直接按序拼上下文。
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    query: str = Field(description="原始查询")
    mode: str = Field(description="检索模式：chunk / qa / hybrid")
    document_id: Optional[str] = Field(default=None, description="被检索的文档 ID")
    chunks: List[ChunkHit] = Field(default_factory=list, description="命中的原文分块")
    qa: List[QAHit] = Field(default_factory=list, description="命中的问答对")
    hits: List[RetrievedHit] = Field(default_factory=list, description="qa 优先的扁平召回视图")
    took_ms: int = Field(default=0, description="耗时（毫秒），含向量化时间")


class DMGuideStatus(BaseModel):
    """剧本的 DM 手册整体状态，用于详情页展示。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    script_id: str = Field(description="剧本 ID")
    has_guide: bool = Field(description="剧本上是否挂了 dmGuide 文件")
    indexed: bool = Field(description="是否已有可检索的活跃文档")
    document_id: Optional[str] = Field(default=None, description="活跃文档 ID")
    file_name: Optional[str] = Field(default=None, description="手册文件名")
    total_pages: int = Field(default=0, description="总页数")
    total_chunks: int = Field(default=0, description="分块数")
    total_qa: int = Field(default=0, description="问答对数")
    version: int = Field(default=0, description="当前版本号")
    job: Optional[JobProgress] = Field(default=None, description="最近一次任务的进度")
