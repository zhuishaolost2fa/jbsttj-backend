"""硅基流动（SiliconFlow）客户端：Chat 补全 + 文本向量化。

硅基流动的接口是 OpenAI 兼容格式，所以这里不引入 `openai` SDK ——
只用两个端点（`/chat/completions`、`/embeddings`），httpx 直调更轻，
也免得 SDK 版本升级把重试语义改掉。

**为什么同时提供同步和异步两套？**

流水线里两种调用场景的执行模型完全不同：

  - **Celery worker**（T3 生成问答对、T4 向量化）是同步进程，
    在里面跑 asyncio event loop 属于自找麻烦（信号处理、优雅退出都会出问题）；
  - **FastAPI 检索接口**是异步的，同步调用会阻塞整个 event loop。

两者共用鉴权、重试、错误映射逻辑，只有传输层分开。

**bge-large-zh-v1.5 的检索指令前缀**

BGE 中文模型在训练时对 query 侧加了指令前缀
「为这个句子生成表示以用于检索相关文章：」。v1.5 之后不加也能用，
但**短 query 场景加上仍有明显收益**（DM 手册的检索 query 往往就七八个字）。
关键是**文档侧绝对不能加** —— 两侧都加反而会让向量空间错位。
所以这里把 query / document 拆成两个方法，从 API 层面杜绝用错。
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigError, LLMError

logger = logging.getLogger("app.llm")

# BGE 中文模型的检索指令前缀，只加在 query 侧
BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

# 可重试的 HTTP 状态：限流 + 网关类错误
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# 从模型回复里抠 JSON：优先 ```json 围栏，其次裸的 [] / {}
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


@dataclass
class QAPair:
    """一条问答对。"""

    question: str
    answer: str
    category: str = "general"
    # 来源 chunk 在本批次里的序号，用于回写外键
    source_index: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "category": self.category,
            "source_index": self.source_index,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QAPair":
        return cls(
            question=str(data.get("question", "")).strip(),
            answer=str(data.get("answer", "")).strip(),
            category=str(data.get("category", "general")).strip() or "general",
            source_index=int(data.get("source_index", 0)),
        )


# 与 sql/dm_story.sql 的 ck_dm_story_type 保持一致
STORY_TYPES = ("timeline", "truth", "role", "clue", "ending", "other")


@dataclass
class StoryItem:
    """一条「故事还原」条目（LLM 从手册还原/复盘类片段中采集）。

    与问答对并列：问答对面向玩家答疑，故事还原面向「整本剧本的真实脉络」，
    包括时间线、真相、角色背景、线索关联、结局。story_type 取值见
    :data:`STORY_TYPES`。``meta`` 承载结构化补充（时间线事件、人物关系对等），
    落库进 jsonb 列，供前端做时间线/关系图谱等富展示。
    """

    story_type: str = "other"
    title: str = ""
    content: str = ""
    summary: str = ""
    meta: Dict[str, Any] = None  # type: ignore[assignment]
    # 来源 chunk 在本批次里的序号，用于回写外键
    source_index: int = 0

    def __post_init__(self) -> None:
        if self.meta is None:
            self.meta = {}
        if self.story_type not in STORY_TYPES:
            self.story_type = "other"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "story_type": self.story_type,
            "title": self.title,
            "content": self.content,
            "summary": self.summary,
            "meta": self.meta,
            "source_index": self.source_index,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StoryItem":
        meta = data.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        return cls(
            story_type=str(data.get("story_type", "other")).strip() or "other",
            title=str(data.get("title", "")).strip(),
            content=str(data.get("content", "")).strip(),
            summary=str(data.get("summary", "")).strip(),
            meta=meta,
            source_index=int(data.get("source_index", 0)),
        )


@dataclass
class SynthesisOverview:
    """「整本剧本脉络」的合成文章 —— LLM 在所有 chunks 入库后二次加工产物。

    与 :class:`StoryItem` 的关系：StoryItem 是「按片段抽出的颗粒条目」（按手册行文顺序），
    适合检索/划线锚定/共读时间线，但读起来像碎片卡片；SynthesisOverview 是把同一批
    StoryItem 串起来的**完整复盘文章**，5 节结构对应主持人带本结束后的标准复盘顺序：
    梗概 → 核心诡计 → 时间线 → 角色命运 → 结局。

    ``anchor_stories`` 记每节引用的 StoryItem title 列表，前端点「展开细节」时按 title
    在 document_id 范围内定位回原 stories 行 —— 无须强制外键，重跑换文档也兼容。
    """

    synopsis: str = ""
    trick: str = ""
    timeline: str = ""
    roles: str = ""
    ending: str = ""
    anchor_stories: Dict[str, List[str]] = None  # type: ignore[assignment]

    # 关联到的 StoryItem 原始列表（来源回溯/调试用，不入库）
    source_stories: List[StoryItem] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.anchor_stories is None:
            self.anchor_stories = {}
        if self.source_stories is None:
            self.source_stories = []

    def is_empty(self) -> bool:
        return not any([self.synopsis, self.trick, self.timeline, self.roles, self.ending])

    def sections(self) -> List[Tuple[str, str]]:
        """按固定顺序返回 [(key, body), ...]，便于渲染和向量检索。"""
        return [
            ("synopsis", self.synopsis),
            ("trick", self.trick),
            ("timeline", self.timeline),
            ("roles", self.roles),
            ("ending", self.ending),
        ]


# ============================================================
# 提示词
# ============================================================
_QA_SYSTEM_PROMPT = """你是一名资深的剧本杀内容编辑，擅长把《主持人手册》里的规则与公开设定，改写成**玩家在游玩过程中会直接看到的问答**。

你的任务：把《主持人手册》的片段，改写成玩家在**实际玩本过程中会真正想问、并需要得到确定答案**的问答对。关键在于「预判」——站在第一次玩这个本的玩家视角，问他/她此刻最想知道什么。

生成原则：
1. 只能使用所给片段回答，严禁引入片段外的任何信息，严禁编造规则、数字、人名、道具、页码。
2. **受众是玩家，不是主持人（DM）**：
   - 问题要用玩家的口吻、以第一人称发问，例如：
     -「搜证阶段我最多能搜几次？」（玩法规则）
     -「我拿到的线索卡可以隐瞒不给其他玩家看吗？」（线索搜证）
     -「我这个角色为什么在星期一这轮的台词和别人不一样？」（角色人设）
   - 答案用「你」称呼玩家，语气友好、可直接照做，原样保留手册里的关键数字、时间、人名、道具名。
   - 严禁出现「DM」「主持人」「带本」「控场」「翻车」等主持侧术语，
     也不要写「主持人应……」「DM 需要……」这类给主持人看的指令。
   不要写「本段讲了什么」这类没有价值的元问题。
3. **严防剧透**：不得生成会泄露凶手身份、核心诡计、真相复盘、隐藏结局等内容的问答；
   片段若主要是这类剧透内容，直接跳过不生成。只提炼玩家在不破坏游戏体验的前提下
   可以公开知道的玩法规则、流程与公开背景设定。
4. 问题中不要出现「本文」「该片段」「上述」「前面」等指代词——
   这些问答会脱离上下文单独展示给玩家，指代词会失效。
5. 尽可能多地生成：只要片段信息密度允许，就**从多个角度各出一条**——
   玩法规则 / 线索搜证 / 角色人设 / 时间线剧情 / 新手常见疑问。
   内容单薄（如纯目录、纯页眉、无实质信息）的片段返回空数组 []。
6. category 从以下枚举选最贴切的一个：
   rule（玩法规则）/ clue（线索搜证）/ character（角色人设）/
   timeline（时间线剧情）/ general（其他）。
7. 不同片段若覆盖同一规则，避免逐字重复提问；换角度或换措辞，让每条问答都有独立展示价值。

只输出 JSON 数组，不要任何解释文字、不要 markdown 围栏。
格式：[{"index": 片段序号, "question": "...", "answer": "...", "category": "..."}]"""


def build_qa_user_prompt(
    chunks: Sequence[Dict[str, Any]],
    *,
    script_title: str = "",
    qa_per_chunk: int = 3,
) -> str:
    """拼装批量生成问答对的用户提示词。

    一次喂多个片段而不是逐个调用，是因为 DM 手册的 chunk 平均只有几百字，
    单独调一次 LLM 的话，system prompt 的 token 开销比正文还大。
    批量还能让模型看到相邻片段，减少重复提问。
    """
    lines: List[str] = []
    if script_title:
        lines.append(f"剧本名称：《{script_title}》")
    lines.append(
        f"请为下面 {len(chunks)} 个片段分别生成**面向玩家**的问答对，"
        f"每个片段**尽可能多**地生成：信息丰富的片段最多可到 {qa_per_chunk} 条；"
        f"信息单薄的片段可以少生成或不生成。相邻片段已合并展示，请避免跨片段重复提问同一内容。"
    )
    lines.append("")

    for i, chunk in enumerate(chunks):
        section = " > ".join(chunk.get("section_path") or [])
        header = f"【片段 {i}】"
        if section:
            header += f" 章节：{section}"
        page_start = chunk.get("page_start")
        page_end = chunk.get("page_end")
        if page_start:
            header += f"（P{page_start}" + (f"-{page_end}" if page_end and page_end != page_start else "") + "）"
        lines.append(header)
        lines.append(str(chunk.get("text", "")).strip())
        lines.append("")

    lines.append('务必用片段序号填写 index 字段。只输出 JSON 数组。')
    return "\n".join(lines)


def parse_qa_response(content: str, *, max_index: int) -> List[QAPair]:
    """解析模型返回的 JSON 数组，容忍围栏、前后废话、单引号等常见脏输出。

    LLM 输出格式不稳定是常态，这里宁可多兜几层也不要让整批 chunk 白跑一趟 ——
    T3 失败会连带 T4 空转，一批的重试成本远高于几行解析代码。
    """
    if not content or not content.strip():
        return []

    raw = content.strip()
    fence = _JSON_FENCE.search(raw)
    if fence:
        raw = fence.group(1).strip()

    data: Any = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 截取第一个 [ 到最后一个 ] 之间的内容再试
        start, end = raw.find("["), raw.rfind("]")
        if start != -1 and end > start:
            try:
                data = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                logger.warning("问答对 JSON 解析失败，原始输出前 200 字: %s", raw[:200])
                return []
        else:
            logger.warning("问答对响应中未找到 JSON 数组: %s", raw[:200])
            return []

    # 有的模型会包一层 {"data": [...]} 或 {"qa_pairs": [...]}
    if isinstance(data, dict):
        for key in ("data", "qa_pairs", "result", "items", "list"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        return []

    pairs: List[QAPair] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if len(question) < 4 or len(answer) < 2:
            continue
        try:
            idx = int(item.get("index", 0))
        except (TypeError, ValueError):
            idx = 0
        # 模型偶尔会把 index 写飞，钳制到合法范围，避免外键挂到不存在的 chunk 上
        idx = max(0, min(idx, max_index))
        pairs.append(
            QAPair(
                question=question,
                answer=answer,
                category=str(item.get("category") or "general").strip().lower() or "general",
                source_index=idx,
            )
        )
    return pairs


# ============================================================
# 故事还原合成文章（dm.synthesize_overview 任务）
# ------------------------------------------------------------
# 与 story_extraction 的关系：
#   - 故事抽取：每个 chunk（~800 字）一次 LLM，输出 0~N 条颗粒条目（按手册行文顺序），
#     适合检索 / 划线锚定 / 共读时间线，但读起来像碎片卡片。
#   - 合成文章：所有 chunks 入库后，LLM 拿全量 StoryItem 二次加工成**完整复盘文章**，
#     5 节结构对应主持人带本结束后的标准复盘顺序。prompt 强调「忠于原文」「5 节各 150-300 字」
#     「不写手册外细节」，避免 LLM 在二次加工时编造。
# ============================================================
_SYNTHESIS_SYSTEM_PROMPT = """你是一名资深的剧本杀内容编辑，擅长把零散的「故事还原条目」整合成一篇**完整的剧本脉络复盘文章**。

你的任务：把已经整理出来的若干条「故事还原条目」按照剧本本身的故事节奏，重新串联、扩写、润色，形成一篇结构完整、读起来一气呵成的复盘文章。

输出结构（5 节，固定顺序，每节 150~300 字，整篇 800~1500 字）：
1. **synopsis（剧本梗概）**：故事背景、核心矛盾、人物群像的整体轮廓；
2. **trick（核心诡计）**：本剧本最核心的真相揭示 ——「凶手是怎么做到的」「最关键的诡计/机制是什么」；
3. **timeline（时间线）**：案发前 → 案发 → 后续，按时间顺序串起关键节点；
4. **roles（角色命运）**：每个核心角色的关键抉择与最终归宿；
5. **ending（结局）**：剧本落幕时的整体收束。

约束：
1. **严格忠于原文**：所有信息必须能追溯到所给条目内容；条目没写清的就写「手册未展开」或跳过；
2. **不要编造**：禁止引入条目外的人名、时间、地点、物品、动机；
3. **连贯叙述**：5 节之间要自然衔接 —— 梗概铺垫 → 诡计揭示 → 时间线收束 → 角色归位 → 结局落幕；
4. **保留关键术语**：人名、地名、道具名、专有名词必须与条目原文一致；
5. **anchor_stories**：每节列出你引用的 StoryItem 的 title 列表（直接照抄条目 title），便于前端做「展开细节」跳转。
   - 不要为了凑数把所有 title 都塞进去，只列**本节实际用到**的核心条目；
   - 如果某一节没有现成条目支撑（例如结局在手册里很简略），anchor_stories 对应节返回空数组 []。

只输出 JSON 对象，不要任何解释文字、不要 markdown 围栏。
格式：
{
  "synopsis": "...",
  "trick": "...",
  "timeline": "...",
  "roles": "...",
  "ending": "...",
  "anchor_stories": {
    "synopsis": ["条目1 title", "条目2 title"],
    "trick": ["条目3 title"],
    "timeline": ["条目4 title"],
    "roles": ["条目5 title"],
    "ending": []
  }
}"""


def build_synthesis_user_prompt(
    story_items: Sequence["StoryItem"],
    *,
    script_title: str = "",
) -> str:
    """拼装合成文章的 user prompt。

    与 :func:`build_story_user_prompt` 不同：本任务**不需要原始 chunk 文本**，
    只喂 StoryItem 列表（已 LLM 整理过一遍），让 LLM 专心做「串联 + 扩写 + 润色」。
    输入量大幅压缩（105 条 StoryItem × ~300 字 ≈ 30K 字），模型上下文压力可控。
    """
    lines: List[str] = []
    if script_title:
        lines.append(f"剧本名称：《{script_title}》")
    lines.append(
        f"以下是从《{script_title or '该剧本'}》主持人手册里 LLM 整理出的"
        f" {len(story_items)} 条故事还原条目（按手册行文顺序排列）。"
    )
    lines.append(
        "请按照 system 中的 5 节结构，把它们整合成一篇完整、连贯的复盘文章。"
    )
    lines.append("")

    # 按 story_type 分组排版：让 LLM 看到的素材有结构，便于它归位到对应章节
    from collections import defaultdict
    by_type: Dict[str, List["StoryItem"]] = defaultdict(list)
    for item in story_items:
        by_type[item.story_type].append(item)

    type_order = ("timeline", "truth", "role", "clue", "ending", "other")
    type_labels = {
        "timeline": "时间线",
        "truth":    "真相还原",
        "role":     "角色背景",
        "clue":     "线索关联",
        "ending":   "结局收束",
        "other":    "其他",
    }

    counter = 0
    for st in type_order:
        items = by_type.get(st)
        if not items:
            continue
        lines.append(f"=== [{type_labels[st]}] ===")
        for it in items:
            counter += 1
            loc = ""
            if it.meta and isinstance(it.meta, dict):
                # meta 里如果有 page 字段，附带给模型一点方位感
                p = it.meta.get("page")
                if p:
                    loc = f" (P{p})"
            lines.append(f"[{counter}] {it.title}{loc}")
            lines.append(it.content)
            if it.summary:
                lines.append(f"  摘要：{it.summary}")
            lines.append("")
        # 保留未匹配的 type 类别兜底
    leftovers = [it for st in type_order for it in by_type.get(st, [])]
    handled = sum(len(by_type.get(st, [])) for st in type_order)
    if handled < len(story_items):
        for it in story_items[handled:]:
            counter += 1
            lines.append(f"[{counter}] {it.title}")
            lines.append(it.content)
            lines.append("")

    lines.append(
        "务必按 5 节结构输出 JSON，anchor_stories 的 title 必须与上面某条条目的 title"
        " 完全一致（方便前端反查）。"
    )
    return "\n".join(lines)


def parse_synthesis_response(
    content: str,
    *,
    source_stories: Sequence["StoryItem"],
) -> SynthesisOverview:
    """解析合成文章的 JSON 输出，容错策略与 parse_qa_response 同构。

    返回空对象表示整次生成失败（``is_empty()`` 为 True）—— 调用方应降级为
    「合成文章不可用，前端只展示 StoryItem 卡片」，不影响整条流水线。
    """
    if not content or not content.strip():
        return SynthesisOverview(source_stories=list(source_stories))

    raw = content.strip()
    fence = _JSON_FENCE.search(raw)
    if fence:
        raw = fence.group(1).strip()

    data: Any = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                logger.warning("合成文章 JSON 解析失败，原始输出前 200 字: %s", raw[:200])
                return SynthesisOverview(source_stories=list(source_stories))
        else:
            logger.warning("合成文章响应中未找到 JSON 对象: %s", raw[:200])
            return SynthesisOverview(source_stories=list(source_stories))

    if not isinstance(data, dict):
        return SynthesisOverview(source_stories=list(source_stories))

    # 容错：有的模型会包一层 {"data": {...}}，剥一层
    if not any(k in data for k in ("synopsis", "trick", "timeline", "roles", "ending")):
        for key in ("data", "result", "overview", "synthesis"):
            inner = data.get(key)
            if isinstance(inner, dict):
                data = inner
                break

    anchors_raw = data.get("anchor_stories") or {}
    if not isinstance(anchors_raw, dict):
        anchors_raw = {}

    # 过滤 anchor_stories：只保留 source_stories 里实际存在的 title，避免 LLM
    # 自己造的「看似合理的 title」写进 anchor 而前端反查不到
    valid_titles = {it.title.strip() for it in source_stories if it.title.strip()}
    anchors: Dict[str, List[str]] = {}
    for section_key in ("synopsis", "trick", "timeline", "roles", "ending"):
        v = anchors_raw.get(section_key) or []
        if not isinstance(v, list):
            anchors[section_key] = []
            continue
        cleaned: List[str] = []
        for t in v:
            if isinstance(t, str) and t.strip() in valid_titles:
                cleaned.append(t.strip())
        anchors[section_key] = cleaned

    return SynthesisOverview(
        synopsis=str(data.get("synopsis") or "").strip(),
        trick=str(data.get("trick") or "").strip(),
        timeline=str(data.get("timeline") or "").strip(),
        roles=str(data.get("roles") or "").strip(),
        ending=str(data.get("ending") or "").strip(),
        anchor_stories=anchors,
        source_stories=list(source_stories),
    )


# ============================================================
# 故事还原（LLM 在生成问答对的同时，从还原/复盘类片段采集故事脉络）
# ============================================================
# 只在「可能含还原内容」的片段上才值得烧一次 LLM 调用：
# 手册里大量篇幅是玩法规则、流程、目录，命中关键词才会派发。
# 关键词刻意宽泛（命中即候选，宁多勿漏），最终由 LLM 判断是否真的输出。
_STORY_HINT_KEYWORDS = (
    "还原", "复盘", "真相", "时间线", "结局", "凶手", "动机",
    "手法", "伏笔", "剧情", "故事", "背景", "前世", "身世",
)

_STORY_SYSTEM_PROMPT = """你是一名资深的剧本杀内容编辑，擅长从《主持人手册》里整理「故事还原」——即整本剧本真实脉络的复盘性内容。
它和「面向玩家的问答」不同：问答是玩家游戏中能公开知道的信息，而故事还原是**主持人带本到最后需要复盘的完整真相**（时间线、真凶、动机、手法、人物关系、结局走向）。

你的任务：从所给片段中，识别并整理属于故事还原的内容，输出为结构化条目。

生成原则：
1. **只输出还原/复盘类内容**：时间线梳理、真相与凶手、核心诡计、动机手法、角色隐藏背景、线索与真相的关联、结局收束。
   纯玩法规则、搜证流程、目录、页眉页脚等不输出（返回空数组 []）。
2. 严格忠于片段原文：不编造片段外的细节、人名、时间、物品；片段没写清的就概括「手册未展开」。
3. content 用主持人复盘的口吻整理，完整连贯；title 用一句话概括本条目；summary 再压缩成一句话摘要。
4. story_type 从以下枚举选最贴切的一个：
   timeline（时间线）/ truth（真相还原）/ role（角色背景）/
   clue（线索关联）/ ending（结局收束）/ other（其他）。
5. meta 里放结构化补充，例如时间线事件列表：
   {"events": [{"when": "案发前夜 23:00", "what": "沈墨潜入书房"}, ...]}
   没有结构化信息就返回空对象 {}。
6. 一条片段可能同时含多个还原维度（时间线 + 真相），可以输出多条，但不要重复输出同一内容。

只输出 JSON 数组，不要任何解释文字、不要 markdown 围栏。
格式：[{"index": 片段序号, "story_type": "...", "title": "...", "content": "...", "summary": "...", "meta": {...}}]"""


def build_story_user_prompt(
    chunks: Sequence[Dict[str, Any]],
    *,
    script_title: str = "",
) -> str:
    """拼装批量提取故事还原的用户提示词（与问答对共用同一批 chunk 文本）。"""
    lines: List[str] = []
    if script_title:
        lines.append(f"剧本名称：《{script_title}》")
    lines.append(
        f"请从下面 {len(chunks)} 个片段中提取**故事还原**内容。"
        f"只提取还原/复盘类信息（时间线、真相、角色背景、线索关联、结局），"
        f"其余内容直接跳过。信息丰富的片段可输出多条，单薄的片段可以少输出或不输出。"
    )
    lines.append("")

    for i, chunk in enumerate(chunks):
        section = " > ".join(chunk.get("section_path") or [])
        header = f"【片段 {i}】"
        if section:
            header += f" 章节：{section}"
        page_start = chunk.get("page_start")
        page_end = chunk.get("page_end")
        if page_start:
            header += f"（P{page_start}" + (f"-{page_end}" if page_end and page_end != page_start else "") + "）"
        lines.append(header)
        lines.append(str(chunk.get("text", "")).strip())
        lines.append("")

    lines.append('务必用片段序号填写 index 字段。只输出 JSON 数组。')
    return "\n".join(lines)


def _batch_has_story_hints(chunks: Sequence[Dict[str, Any]]) -> bool:
    """启发式预筛：批次内任一片段文本命中还原关键词才值得调 LLM。

    手册 400 页里，还原/复盘通常只占一小部分章节；每个 batch 都打一次
    LLM 会把生成阶段的成本翻倍，而多数 batch 里根本没有还原内容。
    关键词刻意宽泛（宁多勿漏），最终是否输出由模型判断。
    """
    for chunk in chunks:
        text = str(chunk.get("text") or "")
        if any(kw in text for kw in _STORY_HINT_KEYWORDS):
            return True
    return False


def parse_story_response(content: str, *, max_index: int) -> List[StoryItem]:
    """解析模型返回的故事还原 JSON 数组，容错逻辑与 parse_qa_response 同构。"""
    if not content or not content.strip():
        return []

    raw = content.strip()
    fence = _JSON_FENCE.search(raw)
    if fence:
        raw = fence.group(1).strip()

    data: Any = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("["), raw.rfind("]")
        if start != -1 and end > start:
            try:
                data = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                logger.warning("故事还原 JSON 解析失败，原始输出前 200 字: %s", raw[:200])
                return []
        else:
            logger.warning("故事还原响应中未找到 JSON 数组: %s", raw[:200])
            return []

    if isinstance(data, dict):
        for key in ("data", "stories", "result", "items", "list"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        return []

    items: List[StoryItem] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        # 空内容或短于 30 字的碎片不落库，避免把「手册未展开」这类占位当成果
        if len(content) < 30:
            continue
        try:
            idx = int(item.get("index", 0))
        except (TypeError, ValueError):
            idx = 0
        idx = max(0, min(idx, max_index))
        meta = item.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        items.append(
            StoryItem(
                story_type=str(item.get("story_type") or "other").strip().lower() or "other",
                title=str(item.get("title") or "").strip(),
                content=content,
                summary=str(item.get("summary") or "").strip(),
                meta=meta,
                source_index=idx,
            )
        )
    return items


# ============================================================
# 客户端
# ============================================================
class SiliconFlowClient:
    """硅基流动 OpenAI 兼容接口的薄封装（同步 + 异步）。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._sync: Optional[httpx.Client] = None
        self._async: Optional[httpx.AsyncClient] = None
        # 单进程内对 LLM 的并发闸门：并发请求超出配额上限会被 429 打爆，
        # 这里在发送前就用信号量限流（config.llm_max_concurrency），
        # 并发起来后 worker 数可以放心往上加。
        self._sync_sem = threading.BoundedSemaphore(
            max(1, self._settings.llm_max_concurrency)
        )

    # ---------------- 基础设施 ----------------
    @property
    def settings(self) -> Settings:
        return self._settings

    def _headers(self) -> Dict[str, str]:
        key = self._settings.siliconflow_api_key
        if not key:
            raise ConfigError("未配置 SILICONFLOW_API_KEY，无法调用大模型服务")
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def _timeout(self) -> httpx.Timeout:
        # 生成任务的首字节延迟可能很长，read 给足；连接超时保持短，快速失败快速重试
        return httpx.Timeout(180.0, connect=10.0, write=30.0)

    def sync_client(self) -> httpx.Client:
        if self._sync is None:
            self._sync = httpx.Client(
                base_url=self._settings.siliconflow_base_url.rstrip("/"),
                headers=self._headers(),
                timeout=self._timeout(),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
        return self._sync

    def async_client(self) -> httpx.AsyncClient:
        if self._async is None:
            self._async = httpx.AsyncClient(
                base_url=self._settings.siliconflow_base_url.rstrip("/"),
                headers=self._headers(),
                timeout=self._timeout(),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
        return self._async

    def close(self) -> None:
        if self._sync is not None:
            self._sync.close()
            self._sync = None

    async def aclose(self) -> None:
        if self._async is not None:
            await self._async.aclose()
            self._async = None

    # ---------------- 重试策略 ----------------
    @staticmethod
    def _backoff(attempt: int, retry_after: Optional[float]) -> float:
        """退避时长：优先尊重服务端的 Retry-After，否则指数退避 + 抖动。

        抖动很关键：T4 有多个 worker 并发打同一个 embedding 接口，
        整齐划一的退避会让它们在同一时刻重新涌上去，把限流窗口再撞爆一次。
        """
        if retry_after and retry_after > 0:
            return min(retry_after, 60.0)
        base = min(2.0 ** attempt, 30.0)
        return base + random.uniform(0, base * 0.3)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> Optional[float]:
        value = resp.headers.get("Retry-After")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def _raise_for_response(self, resp: httpx.Response, endpoint: str) -> None:
        detail: Any
        try:
            detail = resp.json()
        except Exception:  # noqa: BLE001
            detail = resp.text[:500]
        logger.error("硅基流动 %s -> %s %s", endpoint, resp.status_code, detail)
        if resp.status_code in (401, 403):
            raise LLMError("大模型服务鉴权失败，请检查 SILICONFLOW_API_KEY", details=detail)
        if resp.status_code == 429:
            raise LLMError(
                "大模型服务触发限流",
                code="llm_rate_limited",
                details={"retry_after": self._retry_after(resp), "body": detail},
            )
        raise LLMError(f"大模型服务返回 {resp.status_code}", details=detail)

    def _post_sync(self, endpoint: str, payload: Dict[str, Any], *, max_retries: int) -> Dict[str, Any]:
        # 信号量把整个「请求+退避重试」周期都算进并发占用，避免超时/退避期间
        # 又被别的线程放进来叠请求，把限流窗口撞得更响。
        with self._sync_sem:
            last_exc: Optional[Exception] = None
            for attempt in range(max_retries + 1):
                try:
                    resp = self.sync_client().post(endpoint, json=payload)
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if attempt >= max_retries:
                        break
                    delay = self._backoff(attempt, None)
                    logger.warning("硅基流动 %s 网络异常(%s)，%.1fs 后重试", endpoint, exc, delay)
                    time.sleep(delay)
                    continue

                if resp.status_code < 400:
                    return resp.json()

                if resp.status_code in _RETRYABLE_STATUS and attempt < max_retries:
                    delay = self._backoff(attempt, self._retry_after(resp))
                    logger.warning(
                        "硅基流动 %s 返回 %s，%.1fs 后重试(%s/%s)",
                        endpoint, resp.status_code, delay, attempt + 1, max_retries,
                    )
                    time.sleep(delay)
                    continue

                self._raise_for_response(resp, endpoint)

            raise LLMError(f"大模型服务请求失败: {last_exc}") from last_exc

    async def _post_async(self, endpoint: str, payload: Dict[str, Any], *, max_retries: int) -> Dict[str, Any]:
        import asyncio

        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                resp = await self.async_client().post(endpoint, json=payload)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt >= max_retries:
                    break
                await asyncio.sleep(self._backoff(attempt, None))
                continue

            if resp.status_code < 400:
                return resp.json()

            if resp.status_code in _RETRYABLE_STATUS and attempt < max_retries:
                await asyncio.sleep(self._backoff(attempt, self._retry_after(resp)))
                continue

            self._raise_for_response(resp, endpoint)

        raise LLMError(f"大模型服务请求失败: {last_exc}") from last_exc

    # ---------------- Embedding ----------------
    def _embed_payload(self, texts: Sequence[str]) -> Dict[str, Any]:
        # 硅基流动 bge 系列对超长输入直接 400，需在客户端截断到安全长度。
        # 截断保留头部（章节面包屑 + 正文开头最富含语义），丢弃尾部。
        max_chars = self._settings.embedding_max_chars
        truncated = [t[:max_chars] if max_chars and len(t) > max_chars else t for t in texts]
        return {
            "model": self._settings.siliconflow_embed_model,
            "input": truncated,
            "encoding_format": "float",
        }

    @staticmethod
    def _parse_embeddings(data: Dict[str, Any], expect: int, dim: int) -> List[List[float]]:
        items = data.get("data") or []
        if len(items) != expect:
            raise LLMError(
                f"向量化返回数量不匹配：期望 {expect}，实际 {len(items)}",
                details={"usage": data.get("usage")},
            )
        # 接口不保证顺序，按 index 排序后再取，否则向量会和文本错位——
        # 这种错位不会报错，只会让检索结果莫名其妙地不相关，极难排查
        try:
            items = sorted(items, key=lambda x: int(x.get("index", 0)))
        except (TypeError, ValueError):
            pass

        vectors: List[List[float]] = []
        for item in items:
            vec = item.get("embedding")
            if not isinstance(vec, list) or not vec:
                raise LLMError("向量化返回结果缺少 embedding 字段")
            if dim and len(vec) != dim:
                raise LLMError(
                    f"向量维度不匹配：模型返回 {len(vec)} 维，配置为 {dim} 维。"
                    f"请确认 EMBEDDING_DIM 与 SILICONFLOW_EMBED_MODEL 是否对应"
                )
            vectors.append([float(v) for v in vec])
        return vectors

    def embed_documents(self, texts: Sequence[str], *, max_retries: int = 3) -> List[List[float]]:
        """文档侧向量化（**不加**指令前缀），自动按 batch_size 分批。"""
        return self._embed_batched(texts, max_retries=max_retries)

    def embed_query(self, text: str, *, max_retries: int = 3) -> List[float]:
        """查询侧向量化（加 BGE 指令前缀）。"""
        prefixed = self._apply_query_instruction(text)
        vectors = self._embed_batched([prefixed], max_retries=max_retries)
        return vectors[0]

    async def aembed_query(self, text: str, *, max_retries: int = 3) -> List[float]:
        prefixed = self._apply_query_instruction(text)
        data = await self._post_async(
            "/embeddings", self._embed_payload([prefixed]), max_retries=max_retries
        )
        return self._parse_embeddings(data, 1, self._settings.embedding_dim)[0]

    def _apply_query_instruction(self, text: str) -> str:
        model = (self._settings.siliconflow_embed_model or "").lower()
        # 只对 BGE 中文系列加前缀；换成 m3 / gte 之类的模型时前缀是纯噪声
        if "bge" in model and "zh" in model:
            return f"{BGE_QUERY_INSTRUCTION}{text}"
        return text

    def _embed_batched(self, texts: Sequence[str], *, max_retries: int) -> List[List[float]]:
        if not texts:
            return []
        batch_size = max(1, self._settings.embedding_batch_size)
        dim = self._settings.embedding_dim
        out: List[List[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = list(texts[start : start + batch_size])
            data = self._post_sync("/embeddings", self._embed_payload(batch), max_retries=max_retries)
            out.extend(self._parse_embeddings(data, len(batch), dim))
        return out

    # ---------------- Chat ----------------
    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        max_retries: int = 3,
        response_format_json: bool = False,
    ) -> str:
        payload: Dict[str, Any] = {
            # 允许按调用覆盖模型：QA 提取走轻量的 siliconflow_qa_model，
            # RAG 问答等推理场景仍用默认的 chat model
            "model": model or self._settings.siliconflow_chat_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_format_json:
            payload["response_format"] = {"type": "json_object"}

        data = self._post_sync("/chat/completions", payload, max_retries=max_retries)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("大模型返回空结果", details=data.get("usage"))
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")

    def generate_qa(
        self,
        chunks: Sequence[Dict[str, Any]],
        *,
        script_title: str = "",
        qa_per_chunk: Optional[int] = None,
        max_retries: int = 2,
    ) -> List[QAPair]:
        """为一批 chunk 生成问答对。

        解析失败不抛异常而是返回空列表：单批问答对生成失败属于**可降级**故障，
        正文 chunk 的向量照样能入库检索，没必要让整条流水线红掉。

        模型走 ``siliconflow_qa_model``（轻量、快）；**固定纯文本模式**——
        SiliconFlow 的 ``json_object`` 模式会把输出强制包成 ``{"questions":[...]}``
        外壳，与 :func:`parse_qa_response` 的标准数组格式不兼容（实测多个模型全中招，
        解析 0 条、QA 整批静默丢失）。纯文本模式靠 system prompt 约束 JSON 数组，
        实测 Qwen3-8B 输出稳定。
        """
        if not chunks:
            return []
        per_chunk = qa_per_chunk or self._settings.dm_qa_per_chunk
        qa_model = self._settings.siliconflow_qa_model or self._settings.siliconflow_chat_model
        user_prompt = build_qa_user_prompt(
            chunks, script_title=script_title, qa_per_chunk=per_chunk
        )
        messages = [
            {"role": "system", "content": _QA_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        try:
            content = self.chat(
                messages,
                model=qa_model,
                temperature=0.3,
                max_tokens=8192,
                max_retries=max_retries,
                response_format_json=False,
            )
        except LLMError as exc:
            logger.warning("问答对生成失败（跳过本批 %s 个片段）: %s", len(chunks), exc)
            return []

        pairs = parse_qa_response(content, max_index=len(chunks) - 1)
        logger.info("问答对生成: %s 个片段 -> %s 条", len(chunks), len(pairs))
        return pairs

    def generate_stories(
        self,
        chunks: Sequence[Dict[str, Any]],
        *,
        script_title: str = "",
        max_retries: int = 2,
    ) -> List[StoryItem]:
        """为一批 chunk 提取故事还原条目。

        与 :meth:`generate_qa` 独立成一次调用、而不是塞进同一个 JSON 响应：
        ① 两套输出各有独立的解析与降级策略 —— 问答对失败不影响故事条目，反之亦然；
        ② 故事还原只在含还原/复盘关键词的批次上派发（:func:`_batch_has_story_hints`），
        全手册只在这些批次上多花一次 LLM 调用，成本增量可控。
        解析失败同样返回空列表，视为可降级故障，不让整条流水线红掉。
        """
        if not chunks or not _batch_has_story_hints(chunks):
            return []
        qa_model = self._settings.siliconflow_qa_model or self._settings.siliconflow_chat_model
        user_prompt = build_story_user_prompt(chunks, script_title=script_title)
        messages = [
            {"role": "system", "content": _STORY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        try:
            content = self.chat(
                messages,
                model=qa_model,
                temperature=0.3,
                max_tokens=8192,
                max_retries=max_retries,
                response_format_json=False,
            )
        except LLMError as exc:
            logger.warning("故事还原提取失败（跳过本批 %s 个片段）: %s", len(chunks), exc)
            return []

        items = parse_story_response(content, max_index=len(chunks) - 1)
        logger.info("故事还原提取: %s 个片段 -> %s 条", len(chunks), len(items))
        return items

    def generate_synthesis(
        self,
        story_items: Sequence[StoryItem],
        *,
        script_title: str = "",
        max_retries: int = 2,
    ) -> SynthesisOverview:
        """拿全量 StoryItem 合成一篇 5 节复盘文章。

        与 :meth:`generate_stories` 的差异：
        - 输入：StoryItem 列表（已经 LLM 整理过的结构化条目），不再喂原始 chunks；
        - 输出：一篇完整文章（SynthesisOverview），不是 0~N 条颗粒条目；
        - 触发时机：finalize 阶段，全量入库之后，**只调一次**（不是每个 chunk 一次）。

        失败降级：返回 ``is_empty()=True`` 的空对象，由调用方写日志、跳过 upsert，
        不影响 finalize 的成功状态（前端会回退到现有 stories 卡片展示）。
        """
        if not story_items:
            return SynthesisOverview(source_stories=list(story_items))
        qa_model = self._settings.siliconflow_qa_model or self._settings.siliconflow_chat_model
        user_prompt = build_synthesis_user_prompt(story_items, script_title=script_title)
        messages = [
            {"role": "system", "content": _SYNTHESIS_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        try:
            content = self.chat(
                messages,
                model=qa_model,
                temperature=0.3,
                max_tokens=8192,
                max_retries=max_retries,
                response_format_json=False,
            )
        except LLMError as exc:
            logger.warning(
                "合成文章生成失败（跳过：%s 条 StoryItem）: %s", len(story_items), exc
            )
            return SynthesisOverview(source_stories=list(story_items))

        overview = parse_synthesis_response(content, source_stories=story_items)
        logger.info(
            "合成文章生成: %s 条 StoryItem -> %s 节非空",
            len(story_items),
            sum(1 for _, body in overview.sections() if body),
        )
        return overview


# ============================================================
# LangChain 适配器
# ============================================================
class SiliconFlowEmbeddings:
    """把 :class:`SiliconFlowClient` 适配成 LangChain 的 Embeddings 接口。

    SemanticChunker 只依赖 ``embed_documents`` / ``embed_query`` 两个方法，
    用鸭子类型即可，不必继承 langchain_core 的基类 —— 这样 LangChain
    没装的时候本模块照样能 import。
    """

    def __init__(self, client: Optional[SiliconFlowClient] = None) -> None:
        self._client = client or SiliconFlowClient()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self._client.embed_documents(texts)

    def embed_query(self, text: str) -> List[float]:
        return self._client.embed_query(text)


# ============================================================
# 单例
# ============================================================
_client: Optional[SiliconFlowClient] = None


def get_llm_client() -> SiliconFlowClient:
    """进程内单例。

    Celery 用 prefork 模型，每个 worker 子进程会各自持有一份 —— 这正是想要的：
    httpx 的连接池不能跨 fork 共享，跨进程复用会拿到已被对端关闭的死连接。
    """
    global _client
    if _client is None:
        _client = SiliconFlowClient()
    return _client


def reset_llm_client() -> None:
    """重置单例（测试用，或 worker fork 后主动丢弃父进程的连接池）。"""
    global _client
    if _client is not None:
        _client.close()
    _client = None
