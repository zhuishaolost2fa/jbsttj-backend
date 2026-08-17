"""语义分块。

把 :mod:`app.services.pdf_extract` 产出的结构化块切成适合向量化的 chunk。

**为什么不直接上 LangChain 的 SemanticChunker？**

SemanticChunker 的原理是「逐句 embedding，再按相邻句余弦距离找断点」。
400 页手册大约 30 万字、上万个句子，全量跑一遍语义分块光 embedding 就要调上万次接口——
比后面真正入库的 embedding 调用还贵，且大部分开销是浪费的：手册本身有清晰的
章节层级，绝大多数段落顺着标题切就已经足够干净。

所以这里用**两级策略**：

  1. **结构化粗分**（免费）—— 顺着标题层级与段落边界攒 buffer，
     遇到同级或更高级标题就断开，攒到 ``chunk_size`` 就出一块；
  2. **语义细分**（按需）—— 只有粗分后仍超过 ``semantic_threshold`` 的**长块**
     才走 :func:`semantic_split`。这类块通常是「没有小标题的大段叙述」，
     恰恰是最需要语义断点的地方。

实测下来第二级只会命中 3%~8% 的块，成本可控。三级降级链：
语义分块 → RecursiveCharacterTextSplitter → 内置递归切分器（纯 Python，零依赖），
任意一级不可用都会自动下探，不会中断流水线。

每个 chunk 都会带上**章节面包屑**作为向量化前缀。检索时 query 往往是
「第三幕玩家能拿到哪些线索」，正文里未必出现「第三幕」三个字，
把面包屑喂进 embedding 能显著提升这类跨层级问题的召回率。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence

from app.services.pdf_extract import TextBlock, build_section_paths

logger = logging.getLogger("app.chunking")


# ============================================================
# 可选依赖探测
# ============================================================
try:  # pragma: no cover - 取决于运行环境
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    _HAS_LC_SPLITTER = True
except ImportError:  # pragma: no cover
    RecursiveCharacterTextSplitter = None  # type: ignore[assignment]
    _HAS_LC_SPLITTER = False

# 语义分块**不依赖 langchain-experimental**，见下方 semantic_split 的说明。

# 中文断句：在句末标点**之后**切开（零宽后行断言，保留标点本身）
_ZH_SENTENCE_SPLIT = r"(?<=[。！？!?；;…\n])"
_SENTENCE_RE = re.compile(_ZH_SENTENCE_SPLIT)
# 递归切分的分隔符优先级：段落 > 换行 > 句末 > 句中 > 空格 > 硬切
_FALLBACK_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "…", "，", "、", " ", ""]


class EmbeddingsLike(Protocol):
    """LangChain Embeddings 的最小接口，只用于类型标注。"""

    def embed_documents(self, texts: List[str]) -> List[List[float]]:  # pragma: no cover
        ...

    def embed_query(self, text: str) -> List[float]:  # pragma: no cover
        ...


# ============================================================
# 数据结构
# ============================================================
@dataclass
class Chunk:
    """一个待向量化的文本块。"""

    text: str
    page_start: int
    page_end: int
    section_path: List[str] = field(default_factory=list)
    block_type: str = "body"
    chunk_index: int = 0
    # 由哪种策略产出：structural / semantic / recursive / fallback
    split_strategy: str = "structural"

    @property
    def char_count(self) -> int:
        return len(self.text)

    def embedding_text(self) -> str:
        """送去做 embedding 的文本：章节面包屑 + 正文。

        入库存的是 ``text`` 原文，向量化用的是这个带上下文的版本。
        两者分开，避免检索结果里出现一堆重复的面包屑前缀影响阅读。
        """
        if not self.section_path:
            return self.text
        breadcrumb = " > ".join(self.section_path)
        return f"【{breadcrumb}】\n{self.text}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "section_path": list(self.section_path),
            "block_type": self.block_type,
            "chunk_index": self.chunk_index,
            "split_strategy": self.split_strategy,
            "char_count": self.char_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Chunk":
        return cls(
            text=data["text"],
            page_start=int(data.get("page_start", 0)),
            page_end=int(data.get("page_end", 0)),
            section_path=list(data.get("section_path") or []),
            block_type=data.get("block_type", "body"),
            chunk_index=int(data.get("chunk_index", 0)),
            split_strategy=data.get("split_strategy", "structural"),
        )


@dataclass
class ChunkConfig:
    """分块参数。

    独立于 Settings 定义，方便离线测试直接构造，也方便 Celery 任务
    把参数序列化后透传给 worker（worker 侧不必再读一次配置文件）。
    """

    chunk_size: int = 800
    chunk_overlap: int = 120
    semantic_threshold: int = 1600
    min_chunk_chars: int = 40
    breakpoint_percentile: int = 88
    # 单块硬上限：语义细分后仍超长的，直接按字符硬切，兜住 embedding 的入参长度
    hard_max_chars: int = 3000

    @classmethod
    def from_settings(cls, settings: Any) -> "ChunkConfig":
        return cls(
            chunk_size=getattr(settings, "dm_chunk_size", 800),
            chunk_overlap=getattr(settings, "dm_chunk_overlap", 120),
            semantic_threshold=getattr(settings, "dm_semantic_split_threshold", 1600),
            min_chunk_chars=getattr(settings, "dm_min_chunk_chars", 40),
            breakpoint_percentile=getattr(settings, "dm_semantic_breakpoint_percentile", 88),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "semantic_threshold": self.semantic_threshold,
            "min_chunk_chars": self.min_chunk_chars,
            "breakpoint_percentile": self.breakpoint_percentile,
            "hard_max_chars": self.hard_max_chars,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChunkConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ============================================================
# 兜底切分器（零依赖）
# ============================================================
def recursive_split(
    text: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
    separators: Optional[Sequence[str]] = None,
) -> List[str]:
    """内置递归字符切分，行为对齐 RecursiveCharacterTextSplitter。

    按分隔符优先级逐级下探：先用「段落」切，切完还有超长片段就用「换行」再切，
    直到最后一级空串（逐字符硬切）。这样能最大限度保住语义单元的完整性。
    """
    seps = list(separators) if separators is not None else _FALLBACK_SEPARATORS
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    def _split_by(fragment: str, sep_idx: int) -> List[str]:
        if len(fragment) <= chunk_size:
            return [fragment]
        if sep_idx >= len(seps):
            # 分隔符用尽，硬切
            return [fragment[i : i + chunk_size] for i in range(0, len(fragment), chunk_size)]

        sep = seps[sep_idx]
        if sep == "":
            return [fragment[i : i + chunk_size] for i in range(0, len(fragment), chunk_size)]

        # 保留分隔符：中文标点本身是语义的一部分，切掉会让句子读起来不完整
        raw_parts = fragment.split(sep)
        parts: List[str] = []
        for i, p in enumerate(raw_parts):
            if i < len(raw_parts) - 1:
                parts.append(p + sep)
            elif p:
                parts.append(p)

        pieces: List[str] = []
        for p in parts:
            if len(p) > chunk_size:
                pieces.extend(_split_by(p, sep_idx + 1))
            elif p:
                pieces.append(p)
        return pieces

    pieces = _split_by(text, 0)

    # 把小片段回填成接近 chunk_size 的块，并施加 overlap
    merged: List[str] = []
    buf = ""
    for piece in pieces:
        if buf and len(buf) + len(piece) > chunk_size:
            merged.append(buf)
            buf = _tail_overlap(buf, chunk_overlap) + piece
        else:
            buf += piece
    if buf.strip():
        merged.append(buf)

    return [m for m in merged if m.strip()]


def _tail_overlap(text: str, overlap: int) -> str:
    """取尾部 overlap 个字符作为下一块的前缀，尽量从句子边界起始。

    直接按字符数截断会从句子中间开始，overlap 段落读起来是半句话，
    对 embedding 是噪声。这里在截断点附近找最近的句末标点顺延。
    """
    if overlap <= 0 or not text:
        return ""
    tail = text[-overlap:]
    # 在 tail 里找第一个句末标点，从它之后开始
    m = re.search(r"[。！？!?；;\n]", tail)
    if m and m.end() < len(tail):
        return tail[m.end() :]
    return tail


def split_sentences(text: str) -> List[str]:
    """中文优先的断句。

    LangChain 默认的断句正则是 ``(?<=[.?!])\\s+`` —— 它要求标点后必须有空白。
    中文句子之间没有空格，这个正则在中文文本上**一个断点都找不到**，
    整段会被当成单个句子，语义分块直接退化成不分块。这是很多人接上
    SemanticChunker 后发现「没效果」的真正原因。
    """
    parts = [p.strip() for p in _SENTENCE_RE.split(text) if p and p.strip()]
    return parts


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """1 - 余弦相似度。纯 Python 实现，省掉一个 numpy 依赖。"""
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return 1.0 - dot / ((norm_a**0.5) * (norm_b**0.5))


def _percentile(values: Sequence[float], percentile: float) -> float:
    """线性插值分位数（等价于 numpy.percentile 的默认行为）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * max(0.0, min(100.0, percentile)) / 100.0
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def semantic_split(
    text: str,
    embeddings: EmbeddingsLike,
    *,
    percentile: int = 88,
    buffer_size: int = 1,
    min_sentences: int = 4,
) -> List[str]:
    """语义分块：在「话题发生转折」的地方切开。

    这里自己实现而不用 ``langchain_experimental.SemanticChunker``，原因有二：

      1. **依赖地狱** —— langchain-experimental 0.3.x 锁死 ``langchain-core<0.4``，
         与当前的 langchain-core 1.x 直接冲突，pip 会陷入漫长的版本回溯；
      2. **中文断句** —— 它的默认断句正则对中文无效（见 :func:`split_sentences`），
         而能自定义正则的参数在不同小版本里时有时无。

    算法本身并不复杂，照着原论文思路实现即可：

      1. 断句；
      2. 每个句子与前后各 ``buffer_size`` 句拼成一个「上下文窗口」再向量化
         —— 单句太短，向量噪声大，加上邻居才能稳定表征局部话题；
      3. 计算相邻窗口的余弦距离，得到一条「话题变化曲线」；
      4. 取距离的第 ``percentile`` 分位数作为阈值，超过阈值处即为断点。

    用分位数而不是绝对阈值，是因为不同文本的向量距离基准差异很大：
    叙事段落整体距离偏小，规则条款段落整体偏大。分位数天然自适应。
    """
    sentences = split_sentences(text)
    # 句子太少时距离样本不足，分位数没有统计意义，交给递归切分更稳妥
    if len(sentences) < min_sentences:
        return []

    windows: List[str] = []
    for i in range(len(sentences)):
        lo = max(0, i - buffer_size)
        hi = min(len(sentences), i + buffer_size + 1)
        windows.append("".join(sentences[lo:hi]))

    vectors = embeddings.embed_documents(windows)
    if len(vectors) != len(sentences):
        logger.warning("语义分块：向量数量与句子数不匹配，放弃语义切分")
        return []

    distances = [_cosine_distance(vectors[i], vectors[i + 1]) for i in range(len(vectors) - 1)]
    if not distances:
        return []

    threshold = _percentile(distances, percentile)

    pieces: List[str] = []
    start = 0
    for i, distance in enumerate(distances):
        if distance > threshold:
            pieces.append("".join(sentences[start : i + 1]).strip())
            start = i + 1
    tail = "".join(sentences[start:]).strip()
    if tail:
        pieces.append(tail)

    return [p for p in pieces if p]


def _split_text(
    text: str,
    config: ChunkConfig,
    embeddings: Optional[EmbeddingsLike],
) -> tuple[List[str], str]:
    """对超长文本做细分，返回（片段列表, 使用的策略名）。

    优先级：语义分块（需 embeddings）→ RecursiveCharacterTextSplitter → 内置。
    任何一级抛异常都静默降级，绝不能因为切分器抽风就中断整条流水线。
    """
    # 一级：语义分块
    if embeddings is not None:
        try:
            pieces = semantic_split(
                text, embeddings, percentile=config.breakpoint_percentile
            )
            if pieces:
                # 语义断点不保证长度，超长的再用递归切一遍
                out: List[str] = []
                for p in pieces:
                    if len(p) > config.hard_max_chars:
                        out.extend(
                            recursive_split(
                                p,
                                chunk_size=config.chunk_size,
                                chunk_overlap=config.chunk_overlap,
                            )
                        )
                    else:
                        out.append(p)
                return out, "semantic"
        except Exception as exc:  # noqa: BLE001 - 切分失败必须降级而非崩溃
            logger.warning("语义分块失败，降级为递归切分: %s", exc)

    # 二级：LangChain 递归切分
    if _HAS_LC_SPLITTER:
        try:
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=config.chunk_size,
                chunk_overlap=config.chunk_overlap,
                separators=_FALLBACK_SEPARATORS,
                keep_separator=True,
            )
            pieces = [p.strip() for p in splitter.split_text(text) if p.strip()]
            if pieces:
                return pieces, "recursive"
        except Exception as exc:  # noqa: BLE001
            logger.warning("LangChain 递归切分失败，改用内置切分: %s", exc)

    # 三级：内置
    return (
        recursive_split(
            text, chunk_size=config.chunk_size, chunk_overlap=config.chunk_overlap
        ),
        "fallback",
    )


# ============================================================
# 主流程
# ============================================================
@dataclass
class _Buffer:
    """粗分阶段的累积缓冲。"""

    parts: List[str] = field(default_factory=list)
    page_start: int = 0
    page_end: int = 0
    section_path: List[str] = field(default_factory=list)
    block_type: str = "body"
    length: int = 0
    # 自上次 reset 以来真正新增的正文字数，**不含** overlap 结转来的部分。
    # 用它区分「缓冲里有新内容」和「缓冲里只剩上一块的尾巴」——后者若被当成
    # 独立 chunk 落库，就是一条纯粹的重复数据。
    new_chars: int = 0

    def add(self, text: str, page: int, section_path: List[str], block_type: str) -> None:
        # 缓冲为空、或只剩 overlap 结转时，用当前块重置定位信息，
        # 否则章节路径会停留在上一节，导致面包屑张冠李戴
        if not self.parts or self.new_chars == 0:
            self.page_start = page
            self.page_end = page
            self.section_path = list(section_path)
            self.block_type = block_type
        self.page_end = max(self.page_end, page)
        self.parts.append(text)
        self.length += len(text) + 1
        self.new_chars += len(text)

    def text(self) -> str:
        return "\n".join(p for p in self.parts if p).strip()

    def reset(self, carry: str = "") -> None:
        self.parts = [carry] if carry else []
        self.length = len(carry)
        self.new_chars = 0
        self.page_start = self.page_end
        # section_path / block_type 由下一个 add 重置


def chunk_blocks(
    blocks: Sequence[TextBlock],
    *,
    section_paths: Optional[Sequence[Sequence[str]]] = None,
    config: Optional[ChunkConfig] = None,
    embeddings: Optional[EmbeddingsLike] = None,
) -> List[Chunk]:
    """把结构化块切成 chunk 列表。

    :param blocks: 已经过 ``merge_shards`` + ``strip_noise`` + ``calibrate_headings`` 的块
    :param section_paths: 与 blocks 等长的章节路径；不传则内部计算
    :param embeddings: 传入才会启用语义细分，不传则只用递归切分
    """
    cfg = config or ChunkConfig()
    if not blocks:
        return []

    paths = (
        [list(p) for p in section_paths]
        if section_paths is not None
        else build_section_paths(blocks)
    )
    if len(paths) != len(blocks):
        logger.warning(
            "section_paths 长度(%s)与 blocks(%s)不一致，重新计算", len(paths), len(blocks)
        )
        paths = build_section_paths(blocks)

    coarse: List[Chunk] = []
    buf = _Buffer()
    # 当前 chunk 所属的最深标题层级，用于判断「新标题是否应该断开当前块」
    current_level = 0

    def flush(carry_overlap: bool) -> None:
        nonlocal buf
        # 缓冲里只剩上一块结转的 overlap，没有任何新内容 → 直接丢弃。
        # 不加这道判断的话，「超长块拆分后 + 紧接一个新标题」会凭空多出一条重复 chunk。
        if buf.new_chars <= 0:
            buf.reset()
            return
        text = buf.text()
        if not text:
            buf.reset()
            return
        coarse.append(
            Chunk(
                text=text,
                page_start=buf.page_start or 1,
                page_end=buf.page_end or buf.page_start or 1,
                section_path=list(buf.section_path),
                block_type=buf.block_type,
            )
        )
        # 超长块马上要被二级切分器再切一遍，那一步自带 overlap，
        # 这里再结转一次就成了双重重叠，白白多出冗余文本
        oversized = len(text) > cfg.semantic_threshold
        carry = _tail_overlap(text, cfg.chunk_overlap) if (carry_overlap and not oversized) else ""
        buf.reset(carry)

    for block, path in zip(blocks, paths):
        text = block.text.strip()
        if not text:
            continue

        is_heading = block.block_type == "heading" and block.heading_level > 0

        if is_heading:
            # 更高级（更浅）标题 → 上一节结束，强制断开（且不带 overlap：跨章节的
            # 上下文续接没有意义，反而会把上一章的内容污染进下一章）。
            # 同级标题 → 只有当前缓冲已攒够 chunk_size 的一半才断开；否则并入继续攒。
            # 这一条是关键防碎：手册里大量「小标题 + 一两句话」结构，若同级标题一律
            # 断开，会产出满屏不足百字的 heading 型碎块，section_path 也随之失去意义。
            if buf.parts and (
                current_level == 0
                or block.heading_level < current_level
                or (
                    block.heading_level == current_level
                    and buf.length >= cfg.chunk_size // 2
                )
            ):
                flush(carry_overlap=False)
            current_level = block.heading_level
            # 标题本身作为下一块的引导行，不单独成块
            buf.add(text, block.page, list(path), "heading")
            continue

        buf.add(text, block.page, list(path), block.block_type)

        if buf.length >= cfg.chunk_size:
            flush(carry_overlap=True)

    flush(carry_overlap=False)

    # 二级：超长块语义细分
    final: List[Chunk] = []
    semantic_hits = 0
    for chunk in coarse:
        if chunk.char_count <= cfg.semantic_threshold:
            final.append(chunk)
            continue

        semantic_hits += 1
        pieces, strategy = _split_text(chunk.text, cfg, embeddings)
        if not pieces:
            final.append(chunk)
            continue
        for piece in pieces:
            final.append(
                Chunk(
                    text=piece.strip(),
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    section_path=list(chunk.section_path),
                    block_type=chunk.block_type,
                    split_strategy=strategy,
                )
            )

    # 过滤过短块并重新编号。
    # 过短块多半是残留的装饰行、孤立的角色名、纯标题碎块，向量化后会成为
    # 「万物相似」的噪声中心。但直接丢弃会丢内容 —— 改为「优先并入相邻块」：
    # 先并入前一块，放不下则暂存（pending）待并入后一块，都放不下才丢弃。
    result: List[Chunk] = []
    pending: Optional[Chunk] = None  # 暂存的过短块，等待并入后续块

    def _absorb(target: Chunk, extra: Chunk) -> bool:
        """把 extra 拼到 target 末尾；超过硬上限返回 False。"""
        if len(target.text) + len(extra.text) > cfg.hard_max_chars:
            return False
        target.text = f"{target.text}\n{extra.text}"
        target.page_end = max(target.page_end, extra.page_end)
        return True

    for chunk in final:
        chunk.text = chunk.text.strip()

        # 上一轮暂存的过短块，先尝试并入当前块（正文主体在后的场景更常见）
        if pending is not None:
            if _absorb(chunk, pending):
                chunk.page_start = pending.page_start
            elif not (result and _absorb(result[-1], pending)):
                logger.info("过短块无处安放，丢弃 %s 字", len(pending.text))
            pending = None

        if chunk.char_count < cfg.min_chunk_chars:
            # 过短：先并入前一块，否则暂存等后一块。
            # 连续碎块会在「并入当前块 → 仍过短 → 再暂存」的循环里链式累积，
            # 聚成一条够长的块再落库。
            if result and _absorb(result[-1], chunk):
                continue
            pending = chunk
            continue

        if chunk.char_count > cfg.hard_max_chars:
            # 兜底硬切，防止极端情况撑爆 embedding 入参
            for piece in recursive_split(
                chunk.text,
                chunk_size=cfg.chunk_size,
                chunk_overlap=cfg.chunk_overlap,
            ):
                result.append(
                    Chunk(
                        text=piece,
                        page_start=chunk.page_start,
                        page_end=chunk.page_end,
                        section_path=list(chunk.section_path),
                        block_type=chunk.block_type,
                        split_strategy="fallback",
                    )
                )
            continue

        result.append(chunk)

    # 文末残留的过短块：并入最后一块，放不下才丢弃
    if pending is not None:
        if not (result and _absorb(result[-1], pending)):
            logger.info("过短块无处安放，丢弃 %s 字", len(pending.text))

    for i, chunk in enumerate(result):
        chunk.chunk_index = i

    logger.info(
        "分块完成: blocks=%s -> chunks=%s (语义细分命中 %s 块, 平均 %s 字)",
        len(blocks),
        len(result),
        semantic_hits,
        int(sum(c.char_count for c in result) / len(result)) if result else 0,
    )
    return result


def chunks_to_payload(chunks: Sequence[Chunk]) -> List[Dict[str, Any]]:
    """转成可 JSON 序列化的列表，供 Celery 任务间传递。"""
    return [c.to_dict() for c in chunks]


def chunks_from_payload(payload: Sequence[Dict[str, Any]]) -> List[Chunk]:
    return [Chunk.from_dict(d) for d in payload]
