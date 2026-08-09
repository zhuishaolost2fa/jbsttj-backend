"""阿里云 OCR 识别（处理图片型 / 扫描件 PDF）。

DM 主持人手册经常是整页扫描的 PDF：PyMuPDF 抽不到文字层。流水线在提取阶段
发现「某页没有文字块、但有图片」，就把该页渲染成 PNG 交给阿里云
`RecognizeAllText` 识别，再把识别文本接回既有的分块 / 向量化流程。

OCR 文本没有字号、加粗、精确坐标，这里给标题行赋一个**伪字号**，让下游既有
的 `classify_block` / `calibrate_headings` / `build_section_paths` 逻辑照常产出
章节层级，而不是把所有 OCR 文本都当成一坨正文。

鉴权复用阿里云 OSS 的 AccessKey（同账号），签名由官方 Tea SDK 处理。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from app.services.pdf_extract import TextBlock, _is_heading_like

logger = logging.getLogger("app.ocr")

# PyMuPDF 是可选依赖：没装时只在真正要渲染页面时才报错
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

# OCR SDK（alibabacloud_ocr_api20210707）为新增依赖，未装时给出明确提示而非崩在导入处
_OCR_SDK_AVAILABLE = False
try:
    from alibabacloud_ocr_api20210707.client import Client as OcrClient
    from alibabacloud_ocr_api20210707 import models as ocr_models
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_tea_util import models as util_models

    _OCR_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    OcrClient = None  # type: ignore[assignment]

# OCR 文本没有真实字号，给「看起来像标题」的行一个较大的伪字号，
# 给正文一个较小的伪字号，使下游 classify 逻辑能区分出层级。
_OCR_HEADING_SIZE = 16.0
_OCR_BODY_SIZE = 10.5


class OcrUnavailable(RuntimeError):
    """OCR 不可用（SDK 未装或未开通服务）。"""


def get_ocr_client(settings) -> "OcrClient":
    """构建阿里云 OCR 客户端。

    优先用专门的 OCR 密钥，缺省复用 OSS 的 AccessKey（同账号）。
    """
    if not _OCR_SDK_AVAILABLE:
        raise OcrUnavailable(
            "未安装阿里云 OCR SDK，请执行：pip install alibabacloud_ocr_api20210707"
        )
    ak = settings.ocr_access_key_id or settings.oss_access_key_id
    sk = settings.ocr_access_key_secret or settings.oss_access_key_secret
    if not ak or not sk:
        raise OcrUnavailable(
            "缺少 OCR 访问密钥（且无法复用 OSS 密钥），请在 .env 配置 "
            "OCR_ACCESS_KEY_ID / OCR_ACCESS_KEY_SECRET"
        )
    config = open_api_models.Config(access_key_id=ak, access_key_secret=sk)
    config.endpoint = settings.ocr_endpoint
    # 整页扫描图渲染出的 PNG 偏大，识别耗时可能超过 Tea SDK 默认的 10s 读超时，
    # 这里把读超时放宽到 60s、连接超时 10s，避免单页超时把整本 OCR 拖垮。
    config.read_timeout = 60000
    config.connect_timeout = 10000
    return OcrClient(config)


# OCR 接口单图硬上限 8192px，但官方建议走 URL 时图片 < 1.5MB。
# 这里把最长边压到该值以内，既保住中文小字识别所需的清晰度，又避免大页 PNG 超 10MB 被拒。
_MAX_SIDE = 2600


def render_page_png(path: str, page_no: int, dpi: int = 130) -> bytes:
    """把 PDF 指定页渲染成 PNG 二进制（OCR 输入）。

    按 dpi 算矩阵后，若最长边超过 ``_MAX_SIDE`` 则等比缩小，
    防止整页扫描图渲染出超大 PNG 触发接口的体积限制。
    """
    if not _HAS_FITZ:
        raise OcrUnavailable("未安装 PyMuPDF，无法渲染 PDF 页面：pip install pymupdf")
    doc = fitz.open(path)
    try:
        page = doc.load_page(page_no - 1)
        base = dpi / 72.0
        rect = page.rect
        longest = max(rect.width, rect.height) * base
        scale = min(1.0, _MAX_SIDE / longest) if longest > 0 else 1.0
        mat = fitz.Matrix(base * scale, base * scale)
        pix = page.get_pixmap(matrix=mat)
        return pix.tobytes("png")
    finally:
        doc.close()


def ocr_image(client, image_bytes: bytes, ocr_type: str = "General") -> str:
    """识别单张图片的二进制内容，返回识别文本。

    `RecognizeAllText` 的 `Url` 与 `body` 二选一；这里走 `body` 直接传二进制，
    省去先把页面上传到公网 URL 的环节。
    """
    req = ocr_models.RecognizeAllTextRequest(type=ocr_type)
    req.body = image_bytes
    runtime = util_models.RuntimeOptions()
    resp = client.recognize_all_text_with_options(req, runtime)

    data = getattr(resp.body, "data", None)
    if data is None:
        return ""
    # 通用文字识别返回整页文本放在 Data.Content
    content = getattr(data, "content", None)
    if content:
        return content
    # 某些细分类型只返回 WordsInfo 列表，兜底拼接
    words = getattr(data, "words_info", None)
    if words:
        parts = []
        for w in words:
            word = w.word if hasattr(w, "word") else (w.get("word") if isinstance(w, dict) else "")
            if word:
                parts.append(word)
        return "\n".join(parts)
    return ""


def blocks_from_ocr(page_text: str, page_no: int) -> List[TextBlock]:
    """把 OCR 识别到的单页文本转成 TextBlock 列表。

    按行切分；命中标题特征（编号前缀且较短）的当作标题（赋伪字号），
    其余当正文。下游的 `calibrate_headings` 会用全局字号分布再校准一次，
    标题行伪字号 16 > 正文 10.5，会被稳定判成 2 级左右，章节面包屑得以保留。
    """
    blocks: List[TextBlock] = []
    for raw in page_text.splitlines():
        text = raw.strip()
        if not text:
            continue
        if _is_heading_like(text) and len(text) <= 40:
            blocks.append(
                TextBlock(
                    text=text,
                    page=page_no,
                    block_type="heading",
                    heading_level=2,
                    font_size=_OCR_HEADING_SIZE,
                    y_ratio=0.5,
                )
            )
        else:
            blocks.append(
                TextBlock(
                    text=text,
                    page=page_no,
                    block_type="body",
                    heading_level=0,
                    font_size=_OCR_BODY_SIZE,
                    y_ratio=0.5,
                )
            )
    return blocks


def ocr_pdf_pages(
    path: str,
    pages: List[int],
    *,
    client,
    dpi: int = 150,
    ocr_type: str = "General",
) -> Dict[int, str]:
    """批量 OCR 若干页，返回 {页码: 识别文本}。"""
    out: Dict[int, str] = {}
    for pno in pages:
        try:
            png = render_page_png(path, pno, dpi=dpi)
            out[pno] = ocr_image(client, png, ocr_type)
        except Exception as exc:  # noqa: BLE001 - 单页失败不应中断整本
            logger.warning("OCR 第 %s 页失败，跳过: %s", pno, exc)
            out[pno] = ""
    return out
