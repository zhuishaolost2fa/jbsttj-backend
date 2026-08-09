"""PyMuPDF 结构化文本提取。

DM 主持人手册动辄 360-400 页，直接 `page.get_text()` 拿到的是一坨丢失了层级的纯文本：
标题和正文混在一起、跨页段落被硬切、页眉页脚每页重复一遍。这些噪声会一路污染到
向量库，让检索结果充满「第 47 页」「本手册版权归...」这类无意义命中。

因此这里用 `get_text("dict")` 拿到 **block → line → span** 三级结构，再据此还原：

  1. **段落边界** —— 同一 block 内的 line 按行距与结尾标点合并，
     避免把一句话切成七八个 chunk；
  2. **标题层级** —— 用字号相对正文中位数的倍率 + 加粗 + 长度阈值综合判定，
     不依赖 PDF 自带的书签（DM 手册大多是排版软件直接导出的，压根没有书签）；
  3. **多栏排序** —— 按 x 坐标聚类分栏后再逐栏从上到下读，
     否则双栏排版会读成「左一行右一行」的乱码；
  4. **页眉页脚** —— 单个分片看不出规律，交给 :func:`detect_repeating_lines`
     在全局汇总阶段按出现频次统一剥离。

模块内**不做任何 IO 之外的业务判断**，纯函数式，方便离线测试。
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("app.pdf")

# PyMuPDF 是可选依赖：没装时本模块的纯逻辑函数依然可用（离线测试要用），
# 只有真正要读 PDF 的 extract_shard 会抛错。
# 1.24.3 起官方包名改为 pymupdf，旧的 `import fitz` 会打弃用警告，
# 但老版本又只有 fitz，所以按新→旧的顺序探测。
try:  # pragma: no cover - 取决于运行环境
    import pymupdf as fitz

    _HAS_FITZ = True
except ImportError:  # pragma: no cover
    try:
        import fitz  # PyMuPDF < 1.24.3

        _HAS_FITZ = True
    except ImportError:
        fitz = None  # type: ignore[assignment]
        _HAS_FITZ = False


# span.flags 的第 5 位（值 16）表示 bold，这是 MuPDF 的约定
_FLAG_BOLD = 1 << 4

# 结尾像「未完待续」的标点：以这些字符结尾说明句子还没完，下一行应当接上
_UNFINISHED_TAIL = re.compile(r"[，,、；;：:（(\[【“\"'/\\—-]$")
# 中文句末标点
_SENTENCE_END = re.compile(r"[。！？!?；;”\"）)\]】]$")

# 常见的编号标题前缀，命中后即便字号不突出也按标题处理
_HEADING_PATTERNS = (
    re.compile(r"^第[一二三四五六七八九十百零〇\d]+[章节回幕части部分篇]"),
    re.compile(r"^[一二三四五六七八九十]+[、.．]"),
    re.compile(r"^\d+(\.\d+){0,3}[、.．\s]"),
    re.compile(r"^[（(][一二三四五六七八九十\d]+[)）]"),
    re.compile(r"^(Chapter|Part|Section|Act)\s+[\dIVXLC]+", re.IGNORECASE),
)

# 纯页码行：「12」「- 12 -」「第 12 页」「12 / 400」
_PAGE_NUMBER = re.compile(
    r"^\s*(?:[-—–]\s*)?(?:第\s*)?\d{1,4}(?:\s*(?:页|/\s*\d{1,4}))?\s*(?:[-—–])?\s*$"
)

# 列表项前缀
_LIST_PREFIX = re.compile(r"^\s*(?:[•·▪◦●○■□◆▲★*✓]|[-–—]\s|\d+[.)、]\s|[a-zA-Z][.)]\s)")

# 字号低于正文这个倍率的，视为脚注 / 页眉 / 边注，不参与标题判定
_FOOTNOTE_MAX_SIZE_RATIO = 0.92


@dataclass
class TextBlock:
    """一个语义段落（已合并行、已判定类型）。"""

    text: str
    page: int  # 1-based 页码
    block_type: str = "body"  # body / heading / list / table
    heading_level: int = 0  # 0 表示正文；1-6 为标题层级
    font_size: float = 0.0
    is_bold: bool = False
    # 归一化的版面位置，用于页眉页脚判定：0=页顶，1=页底
    y_ratio: float = 0.5
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "page": self.page,
            "block_type": self.block_type,
            "heading_level": self.heading_level,
            "font_size": round(self.font_size, 2),
            "is_bold": self.is_bold,
            "y_ratio": round(self.y_ratio, 4),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TextBlock":
        return cls(
            text=data["text"],
            page=int(data["page"]),
            block_type=data.get("block_type", "body"),
            heading_level=int(data.get("heading_level", 0)),
            font_size=float(data.get("font_size", 0.0)),
            is_bold=bool(data.get("is_bold", False)),
            y_ratio=float(data.get("y_ratio", 0.5)),
        )


@dataclass
class ShardResult:
    """单个分片的提取结果。"""

    shard_index: int
    page_start: int  # 1-based，闭区间
    page_end: int
    blocks: List[TextBlock] = field(default_factory=list)
    # 分片内所有 span 的字号样本，供全局校准标题层级
    font_sizes: List[float] = field(default_factory=list)
    page_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "shard_index": self.shard_index,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "page_count": self.page_count,
            "blocks": [b.to_dict() for b in self.blocks],
            "font_sizes": [round(f, 2) for f in self.font_sizes],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ShardResult":
        return cls(
            shard_index=int(data["shard_index"]),
            page_start=int(data["page_start"]),
            page_end=int(data["page_end"]),
            page_count=int(data.get("page_count", 0)),
            blocks=[TextBlock.from_dict(b) for b in data.get("blocks", [])],
            font_sizes=[float(f) for f in data.get("font_sizes", [])],
        )


# ============================================================
# 分片规划
# ============================================================
def plan_shards(total_pages: int, pages_per_shard: int) -> List[Tuple[int, int]]:
    """把 total_pages 页切成若干 [start, end] 闭区间（1-based）。

    末片不足一片时并入前一片，避免出现「最后一片只有 1 页」这种
    调度开销大于实际工作量的碎片任务。
    """
    if total_pages <= 0:
        return []
    step = max(1, pages_per_shard)
    shards: List[Tuple[int, int]] = []
    start = 1
    while start <= total_pages:
        end = min(start + step - 1, total_pages)
        shards.append((start, end))
        start = end + 1

    # 末片过小（不足半片）且不止一片时，合并进前一片
    if len(shards) >= 2:
        last_size = shards[-1][1] - shards[-1][0] + 1
        if last_size < max(1, step // 2):
            prev_start, _ = shards[-2]
            _, last_end = shards[-1]
            shards = shards[:-2] + [(prev_start, last_end)]
    return shards


# ============================================================
# 版面分析
# ============================================================
def _detect_columns(spans_x: Sequence[float], page_width: float) -> int:
    """粗判分栏数。

    只区分单栏与双栏：DM 手册几乎不会出现三栏以上。
    判据是「有没有相当比例的 block 起始于页面右半区」。
    """
    if not spans_x or page_width <= 0:
        return 1
    mid = page_width / 2
    right = sum(1 for x in spans_x if x > mid * 1.05)
    ratio = right / len(spans_x)
    # 右半区起始的 block 占比在 25%~75% 之间，才认为是真的双栏；
    # 占比过高说明整体右移（页边距大），并非分栏
    return 2 if 0.25 <= ratio <= 0.75 else 1


def _merge_lines(lines: List[Dict[str, Any]]) -> Tuple[str, float, bool]:
    """把 block 内的多行合并成一个段落，返回（文本, 主字号, 是否加粗）。

    合并规则贴合中文排版：
      - 上一行以句末标点结尾、下一行以标题编号开头 → 换行分隔；
      - 上一行以逗号等未完结标点结尾 → 直接接上，不加空格
        （中文不像英文需要空格分词，加了反而在向量化时引入噪声）；
      - 中英混排时若两侧都是 ASCII 字母数字，补一个空格。
    """
    parts: List[str] = []
    sizes: List[float] = []
    bold_chars = 0
    total_chars = 0

    for line in lines:
        spans = line.get("spans") or []
        line_text = "".join(s.get("text", "") for s in spans).strip()
        if not line_text:
            continue
        for s in spans:
            text = s.get("text", "")
            if not text.strip():
                continue
            size = float(s.get("size", 0) or 0)
            sizes.extend([size] * len(text))
            total_chars += len(text)
            if int(s.get("flags", 0)) & _FLAG_BOLD:
                bold_chars += len(text)
        parts.append(line_text)

    if not parts:
        return "", 0.0, False

    merged = parts[0]
    for nxt in parts[1:]:
        prev_tail = merged[-1] if merged else ""
        if _SENTENCE_END.search(merged) and _is_heading_like(nxt):
            merged += "\n"
        elif _UNFINISHED_TAIL.search(merged):
            pass  # 未完结，直接接上
        elif prev_tail.isascii() and prev_tail.isalnum() and nxt[:1].isascii() and nxt[:1].isalnum():
            merged += " "
        merged += nxt

    # 主字号取字符加权众数，避免行内一个大写字母就把整段判成标题
    main_size = statistics.median(sizes) if sizes else 0.0
    is_bold = total_chars > 0 and bold_chars / total_chars >= 0.6
    return merged.strip(), main_size, is_bold


def _line_metrics(line: Dict[str, Any]) -> Tuple[str, float, float]:
    """单行的（文本, 主字号, 加粗字符占比）。"""
    spans = line.get("spans") or []
    text = "".join(s.get("text", "") for s in spans).strip()
    sizes: List[float] = []
    bold_chars = 0
    total_chars = 0
    for s in spans:
        content = s.get("text", "")
        if not content.strip():
            continue
        size = float(s.get("size", 0) or 0)
        sizes.extend([size] * len(content))
        total_chars += len(content)
        if int(s.get("flags", 0)) & _FLAG_BOLD:
            bold_chars += len(content)
    main_size = statistics.median(sizes) if sizes else 0.0
    bold_ratio = bold_chars / total_chars if total_chars else 0.0
    return text, main_size, bold_ratio


# 相邻行字号差异超过这个倍率就认为跨越了排版层级
_SIZE_BREAK_RATIO = 1.15


def _segment_lines(lines: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """把一个 PDF block 内的行按排版层级切成若干段。

    **这一步不能省。** PyMuPDF 的 block 是按版面邻近度聚合的，
    「2.1 要点说明」这样的小标题只要和下面的正文挨得够近，就会被塞进同一个 block。
    若直接把整个 block 当一段处理，标题的大字号会被正文的字符数淹没
    （主字号取的是字符加权中位数），于是标题被判成正文 —— 章节路径整条断掉，
    检索时再也没法按「第几章」定位。

    切分依据两条：

      1. **字号突变** —— 相邻行字号比值超过 ±15%，说明跨越了层级；
      2. **标题特征** —— 上一行句子已收尾，下一行又短又带编号前缀。
    """
    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    prev_size = 0.0
    prev_text = ""

    for line in lines:
        text, size, _bold = _line_metrics(line)
        if not text:
            continue

        should_split = False
        if current and prev_size > 0 and size > 0:
            ratio = size / prev_size
            if ratio >= _SIZE_BREAK_RATIO or ratio <= 1 / _SIZE_BREAK_RATIO:
                should_split = True
            elif _is_heading_like(text) and _SENTENCE_END.search(prev_text):
                should_split = True

        if should_split:
            groups.append(current)
            current = []

        current.append(line)
        prev_size = size
        prev_text = text

    if current:
        groups.append(current)
    return groups


def _is_heading_like(text: str) -> bool:
    """文本是否长得像标题（只看文字特征，不看字号）。"""
    stripped = text.strip()
    if not stripped or len(stripped) > 40:
        return False
    return any(p.search(stripped) for p in _HEADING_PATTERNS)


def classify_block(
    text: str,
    font_size: float,
    is_bold: bool,
    body_size: float,
) -> Tuple[str, int]:
    """判定 block 类型与标题层级。

    返回 (block_type, heading_level)。heading_level 为 0 表示正文。

    字号倍率是主判据，编号前缀与加粗是补充判据 —— 很多手册的二级标题
    与正文同字号，只靠加粗和「3.2」这类编号区分。
    """
    stripped = text.strip()
    if not stripped:
        return "body", 0

    if _LIST_PREFIX.match(stripped):
        return "list", 0

    ratio = font_size / body_size if body_size > 0 else 1.0
    numbered = _is_heading_like(stripped)
    short = len(stripped) <= 40

    # 字号明显小于正文的一律不是标题。
    # 少了这条，running head「第三章 搜证阶段」（8pt 小字）会因为命中编号前缀
    # 被兜底判成标题，然后混进章节栈，让后面每一块的面包屑都多挂一截幽灵章节。
    if body_size > 0 and ratio < _FOOTNOTE_MAX_SIZE_RATIO:
        return "body", 0

    # 字号显著大于正文 → 按倍率定层级
    if ratio >= 1.6 and short:
        return "heading", 1
    if ratio >= 1.35 and short:
        return "heading", 2
    if ratio >= 1.15 and short:
        return "heading", 3
    # 同字号但有编号前缀 → 靠加粗与长度兜底判定
    if numbered and short and (is_bold or ratio >= 1.05):
        return "heading", 4
    if numbered and len(stripped) <= 24:
        return "heading", 5
    if is_bold and short and ratio >= 1.0 and _SENTENCE_END.search(stripped) is None:
        return "heading", 6
    return "body", 0


def is_noise_text(text: str) -> bool:
    """纯噪声行：页码、单个符号、空白。"""
    stripped = text.strip()
    if not stripped:
        return True
    if _PAGE_NUMBER.match(stripped):
        return True
    # 去掉标点后没剩几个字符
    letters = re.sub(r"[\s\W_]+", "", stripped, flags=re.UNICODE)
    return len(letters) <= 1


# ============================================================
# 核心提取
# ============================================================
def open_document(path: str):
    """打开 PDF，返回 fitz.Document。"""
    if not _HAS_FITZ:
        raise RuntimeError(
            "未安装 PyMuPDF，无法解析 PDF。请执行：pip install pymupdf"
        )
    return fitz.open(path)


def probe_page_count(path: str) -> int:
    """只读页数，用于规划分片。"""
    doc = open_document(path)
    try:
        return doc.page_count
    finally:
        doc.close()


def extract_shard(
    path: str,
    *,
    shard_index: int,
    page_start: int,
    page_end: int,
) -> ShardResult:
    """提取 [page_start, page_end] 闭区间（1-based）的结构化文本。

    每个分片独立 open 一次文档。PyMuPDF 的 Document 对象不是线程/进程安全的，
    也无法跨进程传递，所以不做复用 —— open 本身只解析交叉引用表，开销很小，
    真正的成本在逐页渲染文本。
    """
    doc = open_document(path)
    result = ShardResult(shard_index=shard_index, page_start=page_start, page_end=page_end)

    try:
        total = doc.page_count
        lo = max(1, page_start)
        hi = min(total, page_end)
        result.page_count = max(0, hi - lo + 1)

        raw_blocks: List[Tuple[int, float, float, float, str, float, bool]] = []
        # (page, sort_key_col, y0, x0, text, size, bold)

        for page_no in range(lo, hi + 1):
            page = doc.load_page(page_no - 1)
            page_rect = page.rect
            page_h = float(page_rect.height) or 1.0
            page_w = float(page_rect.width) or 1.0

            try:
                data = page.get_text("dict")
            except Exception as exc:  # noqa: BLE001 - 单页解析失败不应中断整片
                logger.warning("第 %s 页解析失败，跳过: %s", page_no, exc)
                continue

            page_blocks: List[Tuple[float, float, str, float, bool]] = []
            xs: List[float] = []

            for blk in data.get("blocks", []):
                # type != 0 是图片块，DM 手册里的插图暂不做 OCR
                if blk.get("type") != 0:
                    continue
                lines = blk.get("lines") or []
                block_bbox = blk.get("bbox") or (0, 0, 0, 0)

                # 先按排版层级把 block 拆成段，避免标题被正文淹没
                for segment in _segment_lines(lines):
                    text, size, bold = _merge_lines(segment)
                    if not text or is_noise_text(text):
                        continue

                    # 段的位置取自身首行，取不到再退回 block 的 bbox
                    seg_bbox = segment[0].get("bbox") if segment else None
                    if seg_bbox and len(seg_bbox) >= 2:
                        x0, y0 = float(seg_bbox[0]), float(seg_bbox[1])
                    else:
                        x0, y0 = float(block_bbox[0]), float(block_bbox[1])

                    xs.append(x0)
                    page_blocks.append((y0, x0, text, size, bold))

                    # 字号样本按字符数加权：正文段落字多、权重高，
                    # 页眉页脚和标题字少、权重低。这样中位数才能稳定落在正文字号上，
                    # 不会因为「每页两行页眉」就把基准拽偏。
                    weight = min(20, max(1, len(text) // 10))
                    result.font_sizes.extend([size] * weight)

            if not page_blocks:
                continue

            # 分栏排序：双栏时先左栏整列、再右栏整列
            columns = _detect_columns(xs, page_w)
            if columns == 2:
                mid = page_w / 2
                page_blocks.sort(key=lambda b: (0 if b[1] <= mid else 1, b[0], b[1]))
            else:
                page_blocks.sort(key=lambda b: (b[0], b[1]))

            for y0, x0, text, size, bold in page_blocks:
                raw_blocks.append((page_no, 0.0, y0 / page_h, x0, text, size, bold))

        if not raw_blocks:
            return result

        # 分片内的正文字号基准：取字号的中位数。
        # 用中位数而非众数，是因为部分手册正文夹杂大量小字注释，众数会被带偏。
        body_size = statistics.median(result.font_sizes) if result.font_sizes else 0.0

        for page_no, _, y_ratio, x0, text, size, bold in raw_blocks:
            block_type, level = classify_block(text, size, bold, body_size)
            result.blocks.append(
                TextBlock(
                    text=text,
                    page=page_no,
                    block_type=block_type,
                    heading_level=level,
                    font_size=size,
                    is_bold=bold,
                    y_ratio=y_ratio,
                )
            )
        return result
    finally:
        doc.close()


# ============================================================
# 全局阶段：页眉页脚剥离 + 标题层级校准
# ============================================================
# 字号超过正文这个倍率的块，一律不当页眉页脚处理
_HEADER_MAX_SIZE_RATIO = 1.15

# 与最贴边那一行的纵向间距在此范围内，算同属一组页眉/页脚（容纳多行页眉）
_EDGE_GROUP_GAP = 0.03


def _outermost(
    items: List[TextBlock], edge_band: float, gap: float = _EDGE_GROUP_GAP
) -> List[TextBlock]:
    """挑出一页里真正「贴边」的块 —— 顶部最上面那一撮 + 底部最下面那一撮。

    这条判据补的是一个很容易忽略的漏洞：边缘带是个区间，不是一条线。
    上边距小的手册，**正文第一行**同样落在 y_ratio 0.09 附近的带内；
    若这段正文恰好每页都重复（提示框、章节导语），它的重复次数和分布密度
    与 running head 一模一样，会被连同页眉一起剥掉 —— 这是真实的内容丢失，
    而且不报错、不留痕，只有逐块比对才发现得了。

    区分它们靠的是版面的物理事实：**页眉就是页面最上面那一行**。
    正文首行的头顶上还压着页眉，就不该是候选。
    留 ``gap`` 的余量是为了兼容「章节名 + 分隔线」这类两三行的页眉。
    """
    tops = [b for b in items if b.y_ratio <= edge_band]
    bottoms = [b for b in items if b.y_ratio >= (1 - edge_band)]

    picked: List[TextBlock] = []
    if tops:
        edge = min(b.y_ratio for b in tops)
        picked.extend(b for b in tops if b.y_ratio - edge <= gap)
    if bottoms:
        edge = max(b.y_ratio for b in bottoms)
        picked.extend(b for b in bottoms if edge - b.y_ratio <= gap)
    return picked


def detect_repeating_lines(
    blocks: Iterable[TextBlock],
    *,
    total_pages: int,
    ratio_threshold: float = 0.6,
    edge_band: float = 0.12,
    body_size: Optional[float] = None,
    min_density: float = 0.45,
) -> set:
    """找出页眉页脚，返回归一化后的文本集合，调用方据此过滤。

    三道判据缺一不可：

    1. **位置** —— 只看版面上下边缘带，避免误伤正文里反复出现的短句（如角色名）。

    2. **字号** —— 页眉页脚有个几乎不会破的物理规律：**字号不会比正文大**，
       没人会用比正文还大的字去写页码。少了这条，手册里「2.1 要点说明」这类
       恰好排在页顶 8% 处的小节标题，数字归一化后每页长得一模一样，
       位置和频次都与 running head 无法区分，整条章节层级会被连根剥掉。

    3. **连续密度** —— 这里不能只用「占全书页数的比例」。真实手册的 running head
       大多是**章节名**，一本 400 页的书分 20 章，每个页眉只在自己那 20 页里出现，
       占比 5%，任何合理的全局阈值都抓不到它。但它有个更强的特征：
       **在它出现的页码区间内，几乎每一页都有**。所以改判
       `出现页数 / 页码跨度 >= min_density`。默认 0.45 是为了兼容
       「只在奇数页排页眉」的双面印刷版式（密度恰好 0.5）。

    ``ratio_threshold`` 作为兜底保留：出现得足够频繁的文本，即便分布稀疏也算噪声。
    """
    if total_pages <= 0:
        return set()

    block_list = list(blocks)
    if body_size is None:
        sizes = [b.font_size for b in block_list if b.font_size > 0]
        body_size = statistics.median(sizes) if sizes else 0.0
    size_ceiling = body_size * _HEADER_MAX_SIZE_RATIO if body_size > 0 else 0.0

    # 先按页收集边缘带内、字号不超上限的块，再逐页挑出「真正贴边」的那一撮
    per_page: Dict[int, List[TextBlock]] = {}
    for b in block_list:
        if b.y_ratio > edge_band and b.y_ratio < (1 - edge_band):
            continue
        if size_ceiling > 0 and b.font_size > size_ceiling:
            continue  # 比正文还大的字，是标题不是页眉
        per_page.setdefault(b.page, []).append(b)

    # 文本 -> 出现在哪些页
    edge_pages: Dict[str, set] = {}
    for page, items in per_page.items():
        for b in _outermost(items, edge_band):
            key = normalize_for_compare(b.text)
            if not key or len(key) > 120:
                continue
            edge_pages.setdefault(key, set()).add(page)

    # 全局兜底阈值
    global_threshold = max(2, int(total_pages * ratio_threshold))
    # 局部（章节级）页眉的最小重复次数：太小会把偶发重复误判成版式
    local_threshold = max(3, int(total_pages * 0.05))

    hits = set()
    for text, pages in edge_pages.items():
        count = len(pages)
        if count >= global_threshold:
            hits.add(text)
            continue
        if count < local_threshold:
            continue
        span = max(pages) - min(pages) + 1
        if span > 0 and count / span >= min_density:
            hits.add(text)
    return hits


def normalize_for_compare(text: str) -> str:
    """比较用的归一化：抹掉空白、页码数字与全半角差异。

    页眉「如是我观 · 主持人手册 · 第 12 页」在每页都不同（页码在变），
    把连续数字统一替换成占位符后才能识别出它们是同一个页眉。
    """
    import unicodedata

    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"\s+", "", normalized)
    normalized = re.sub(r"\d+", "#", normalized)
    return normalized.strip().lower()


def calibrate_headings(blocks: List[TextBlock]) -> None:
    """全局校准标题层级（原地修改）。

    单个分片内的字号中位数可能被局部排版带偏（例如某片恰好全是大字号的角色卡）。
    汇总后按全局字号分布重新判定，并把层级压缩成连续的 1..N，
    免得出现「只有 1 级和 4 级」这种断层，影响章节路径的可读性。
    """
    if not blocks:
        return

    sizes = [b.font_size for b in blocks if b.font_size > 0]
    if not sizes:
        return
    body_size = statistics.median(sizes)

    for b in blocks:
        block_type, level = classify_block(b.text, b.font_size, b.is_bold, body_size)
        b.block_type = block_type
        b.heading_level = level

    # 压缩层级：把实际出现的层级映射成 1,2,3...
    used = sorted({b.heading_level for b in blocks if b.heading_level > 0})
    if not used:
        return
    remap = {old: new for new, old in enumerate(used, start=1)}
    for b in blocks:
        if b.heading_level > 0:
            b.heading_level = remap[b.heading_level]


def merge_shards(shards: Sequence[ShardResult]) -> Tuple[List[TextBlock], int]:
    """按页序拼接所有分片，返回（有序块列表, 总页数）。

    还会修复**跨分片边界被截断的段落**：如果前一片的最后一块以未完结标点收尾，
    且后一片的第一块不是标题，就把两者合并 —— 400 页手册按 20 页切片会产生
    19 个边界，不修复的话就是 19 个残缺的句子。
    """
    ordered = sorted(shards, key=lambda s: s.page_start)
    blocks: List[TextBlock] = []
    total_pages = 0

    for shard in ordered:
        total_pages = max(total_pages, shard.page_end)
        if not shard.blocks:
            continue
        if blocks:
            tail = blocks[-1]
            head = shard.blocks[0]
            same_flow = (
                tail.block_type == "body"
                and head.block_type == "body"
                and not _SENTENCE_END.search(tail.text)
                and head.page - tail.page <= 1
            )
            if same_flow:
                tail.text = tail.text + head.text
                blocks.extend(shard.blocks[1:])
                continue
        blocks.extend(shard.blocks)

    return blocks, total_pages


def strip_noise(
    blocks: List[TextBlock],
    *,
    total_pages: int,
    ratio_threshold: float = 0.6,
    edge_band: float = 0.12,
) -> Tuple[List[TextBlock], int]:
    """剥离页眉页脚与噪声块，返回（保留的块, 丢弃数量）。

    命中模板只是必要条件。落地删除时必须复用与检测**完全相同**的位置/字号约束，
    否则会出现「检测时很克制、删除时一刀切」的错配：

      - 很多手册拿章节名做 running head，页眉「第三章 搜证阶段」与正文里那个
        大字号的章标题文本一模一样，只按文本匹配会把真标题一起删掉；
      - 每页重复的正文段落（提示框、章节导语）文本也会命中模板，
        但它不在页面最外沿，属于内容而非版式。
    """
    sizes = [b.font_size for b in blocks if b.font_size > 0]
    body_size = statistics.median(sizes) if sizes else 0.0

    repeating = detect_repeating_lines(
        blocks,
        total_pages=total_pages,
        ratio_threshold=ratio_threshold,
        edge_band=edge_band,
        body_size=body_size,
    )
    if repeating:
        logger.info("识别到 %s 条页眉页脚模板，将从正文中剥离", len(repeating))

    # 用与检测一致的口径，圈定「允许被当作版式删掉」的块
    removable = _removable_ids(blocks, edge_band=edge_band, body_size=body_size)

    kept: List[TextBlock] = []
    dropped = 0
    for b in blocks:
        if is_noise_text(b.text):
            dropped += 1
            continue
        if (
            repeating
            and id(b) in removable
            and normalize_for_compare(b.text) in repeating
        ):
            dropped += 1
            continue
        kept.append(b)
    return kept, dropped


def _removable_ids(
    blocks: Sequence[TextBlock], *, edge_band: float, body_size: float
) -> set:
    """圈出可作为页眉页脚删除的块 id —— 与 detect_repeating_lines 同口径。"""
    size_ceiling = body_size * _HEADER_MAX_SIZE_RATIO if body_size > 0 else 0.0
    per_page: Dict[int, List[TextBlock]] = {}
    for b in blocks:
        if b.y_ratio > edge_band and b.y_ratio < (1 - edge_band):
            continue
        if size_ceiling > 0 and b.font_size > size_ceiling:
            continue
        per_page.setdefault(b.page, []).append(b)

    removable = set()
    for items in per_page.values():
        for b in _outermost(items, edge_band):
            removable.add(id(b))
    return removable


def build_section_paths(blocks: Sequence[TextBlock]) -> List[List[str]]:
    """为每个块计算所处的章节路径（面包屑）。

    维护一个标题栈：遇到 N 级标题就把栈截断到 N-1 层再压入。
    检索命中后前端可以直接展示「第三章 > 3.2 关键物证」，
    比只给个页码有用得多。
    """
    paths: List[List[str]] = []
    stack: List[Tuple[int, str]] = []

    for b in blocks:
        if b.block_type == "heading" and b.heading_level > 0:
            while stack and stack[-1][0] >= b.heading_level:
                stack.pop()
            stack.append((b.heading_level, b.text.strip()[:120]))
        paths.append([title for _, title in stack])
    return paths
