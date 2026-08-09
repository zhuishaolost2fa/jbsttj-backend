"""离线验证 OCR 兜底接线（桩客户端，无需真实开通阿里云 OCR 服务）。

模拟场景：传入一份图片型 PDF（无文字层），`extract_shard` 抽不到文字，
触发 OCR 兜底——这里用桩客户端替换阿里云 OCR，返回固定的中文识别文本，
确认最终能产出带标题层级的结构化块，并接进既有分块流程。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings
from app.services.pdf_extract import (
    ShardResult,
    build_section_paths,
    calibrate_headings,
    extract_shard,
    merge_shards,
    strip_noise,
)
from app.services import ocr as ocr_mod

# 桩：模拟扫描页被识别出来的中文文本（含章节标题编号，用于验证层级判定）
SAMPLE_TEXT = """第一章 案件背景
这是一起发生在暴风雪山庄中的密室杀人案。
玩家需要在三幕之内找出真凶。
1.1 角色介绍
每位玩家对应一位嫌疑人，拥有隐藏动机。
2.1 流程要点
主持人应在开场前完成所有线索卡的分发。"""


class _FakeData:
    def __init__(self, content):
        self.content = content


class _FakeBody:
    def __init__(self, content):
        self.data = _FakeData(content)


class _FakeResp:
    def __init__(self, content):
        self.body = _FakeBody(content)


class _FakeClient:
    def recognize_all_text_with_options(self, req, runtime):
        return _FakeResp(SAMPLE_TEXT)


def main() -> None:
    pdf = r"C:\Users\Administrator\Desktop\剧本杀文档\病娇男孩的精分日记..pdf"
    settings = get_settings()

    # 1) 纯文本提取：图片型 PDF 应拿不到任何块
    result = extract_shard(pdf, shard_index=0, page_start=1, page_end=20)
    text_blocks = len(result.blocks)
    print("text-extracted blocks =", text_blocks)

    # 2) 复刻 extract_shard 任务里的 OCR 兜底逻辑（用桩客户端）
    pages_with_text = {b.page for b in result.blocks}
    need_ocr = [p for p in range(1, 21) if p not in pages_with_text]
    print("pages needing OCR  =", len(need_ocr))

    client = _FakeClient()
    ocr_hits = 0
    for pno in need_ocr:
        png = ocr_mod.render_page_png(pdf, pno, settings.ocr_dpi)
        assert isinstance(png, bytes) and len(png) > 0, "渲染 PNG 应非空"
        text = ocr_mod.ocr_image(client, png, settings.ocr_type)
        if text:
            result.blocks.extend(ocr_mod.blocks_from_ocr(text, pno))
            ocr_hits += 1
    print("OCR-backed blocks  =", len(result.blocks), "(hits=%d)" % ocr_hits)
    assert ocr_hits > 0, "OCR 桩应至少命中一页"

    # 3) 下游：合并 / 去噪 / 校准标题层级 / 章节路径
    blocks, total_pages = merge_shards([result])
    blocks, dropped = strip_noise(
        blocks, total_pages=total_pages, ratio_threshold=settings.dm_header_footer_ratio
    )
    calibrate_headings(blocks)
    paths = build_section_paths(blocks)
    headings = [b for b in blocks if b.block_type == "heading"]

    print(
        "after pipeline: blocks=%d headings=%d dropped_noise=%d"
        % (len(blocks), len(headings), dropped)
    )
    print("sample headings :", [b.text for b in headings][:5])
    print("sample paths    :", paths[:3])
    assert len(blocks) > 0, "OCR 兜底后不应为空"
    assert len(headings) > 0, "桩文本里的编号标题应被判为 heading"
    print("OK: OCR 兜底接通，图片型 PDF 可转为带层级的结构化块")


if __name__ == "__main__":
    main()
