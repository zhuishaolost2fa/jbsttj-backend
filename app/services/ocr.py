"""OCR 识别（处理图片型 / 扫描件 PDF）。

DM 主持人手册经常是整页扫描的 PDF：PyMuPDF 抽不到文字层。流水线在提取阶段
发现「某页没有文字块、但有图片」，就把该页渲染成 PNG 交给**本地免费 OCR 引擎**
（`rapid` / `paddle` / `tesseract`，由 `OCR_ENGINE` 配置）识别，再把识别文本
接回既有的分块 / 向量化流程。

**阿里云 OCR 已禁用**：任何路径都不再调用阿里云 `RecognizeAllText`（`ocr_image`
遇到 `aliyun` 直接抛 `OcrUnavailable`，`get_ocr_client` 也不再构建付费客户端），
以避免产生对外付费请求。本地引擎未安装时 OCR 会优雅跳过，扫描页拿不到文字层。

OCR 文本没有字号、加粗、精确坐标，这里给标题行赋一个**伪字号**，让下游既有
的 `classify_block` / `calibrate_headings` / `build_section_paths` 逻辑照常产出
章节层级，而不是把所有 OCR 文本都当成一坨正文。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

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

    ⚠️ 阿里云 OCR 已禁用：本函数不再对外提供服务，调用即抛错，
    以确保任何代码路径都无法触发付费的 ``RecognizeAllText`` 请求。
    需要 OCR 时请改用本地免费引擎（OCR_ENGINE=rapid/paddle/tesseract）。
    """
    raise OcrUnavailable(
        "阿里云 OCR 已禁用，无法构建阿里云 OCR 客户端；"
        "请改用本地引擎（rapid/paddle/tesseract）"
    )


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


# ============================================================
# 本地免费 OCR 引擎（替代阿里云，离线运行，零成本）
# ============================================================
# 引擎实例缓存：rapid / paddle 首次调用会加载模型，之后复用，避免逐页重载。
_LOCAL_ENGINES: Dict[str, Any] = {}


def _local_engine(engine: str):
    """惰性创建并缓存本地 OCR 引擎实例。仅导入对应库，缺失时给出清晰报错。"""
    key = engine.lower()
    if key in _LOCAL_ENGINES:
        return _LOCAL_ENGINES[key]
    if key == "rapid":
        from rapidocr_onnxruntime import RapidOCR

        _LOCAL_ENGINES[key] = RapidOCR()
    elif key == "paddle":
        from paddleocr import PaddleOCR

        _LOCAL_ENGINES[key] = PaddleOCR(
            use_angle_cls=True, lang="ch", show_log=False, use_gpu=False
        )
    elif key == "tesseract":
        import pytesseract

        _LOCAL_ENGINES[key] = pytesseract
    else:
        raise OcrUnavailable(f"不支持的本地 OCR 引擎: {engine}")
    return _LOCAL_ENGINES[key]


def local_ocr_available(engine: str) -> bool:
    """仅检查依赖是否安装（不加载模型），供调用方提前判断能否走本地 OCR。"""
    try:
        if engine.lower() == "rapid":
            import rapidocr_onnxruntime  # noqa: F401
        elif engine.lower() == "paddle":
            import paddleocr  # noqa: F401
        elif engine.lower() == "tesseract":
            import pytesseract  # noqa: F401
        else:
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _ocr_image_local(image_bytes: bytes, engine: str) -> str:
    """本地免费引擎识别单张图片，返回整页文本。复用上游的 render_page_png 输入。"""
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    key = engine.lower()

    if key == "rapid":
        import numpy as np

        result, _ = _local_engine("rapid")(np.array(img))
        if not result:
            return ""
        return "\n".join(line[1] for line in result if line and len(line) > 1)

    if key == "paddle":
        result = _local_engine("paddle")(img) or []
        lines: List[str] = []
        for item in result:
            if item and len(item) >= 2 and item[1] and len(item[1]) >= 2:
                lines.append(item[1][0])
        return "\n".join(lines)

    if key == "tesseract":
        return _local_engine("tesseract").image_to_string(img, lang="chi_sim+eng")

    raise OcrUnavailable(f"不支持的本地 OCR 引擎: {engine}")


def ocr_image(client, image_bytes: bytes, ocr_type: str = "General", *, engine: Optional[str] = None) -> str:
    """识别单张图片的二进制内容，返回识别文本。

    引擎由 ``OCR_ENGINE`` 配置决定（默认 ``rapid``，本地离线免费）：

      - ``rapid`` / ``paddle`` / ``tesseract`` → 本地离线免费引擎，
        不需要 ``client``，也不产生任何对外请求；
      - ``aliyun`` → **已禁用**，调用即抛 ``OcrUnavailable``，绝不触发付费云识别。

    注意：**阿里云 OCR 已禁用**，``client`` 参数在此处不再有意义（``get_ocr_client``
    也已改为直接抛错），请勿再传入阿里云客户端。
    """
    if engine is None:
        from app.core.config import get_settings

        engine = (get_settings().ocr_engine or "rapid").lower()
    if engine == "aliyun":
        # 阿里云 OCR 已禁用（见 OCR_ENGINE 配置约束）：任何途径都不得触发付费云识别。
        raise OcrUnavailable(
            "阿里云 OCR 已禁用，OCR_ENGINE 请改用本地引擎（rapid/paddle/tesseract）"
        )
    return _ocr_image_local(image_bytes, engine)


def _ocr_image_aliyun(client, image_bytes: bytes, ocr_type: str = "General") -> str:
    """（原 ocr_image）阿里云 RecognizeAllText 实现。"""
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
