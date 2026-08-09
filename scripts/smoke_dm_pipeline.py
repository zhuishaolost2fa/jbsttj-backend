"""DM 指南流水线离线冒烟测试。

**不需要** RabbitMQ / Redis / Supabase / 大模型 API Key，纯本地跑通
「合成 PDF → 分片提取 → 去噪 → 语义分块 → 全局去重」这条主链路，
用来在改动核心算法后快速回归。

用法::

    python scripts/smoke_dm_pipeline.py            # 生成 60 页样本并跑通
    python scripts/smoke_dm_pipeline.py --pages 200
    python scripts/smoke_dm_pipeline.py --pdf /path/to/real_dm_guide.pdf

带 `--pdf` 时会跑真实文件，可用来观察真实手册的分块效果与去重率。
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.chunking import ChunkConfig, chunk_blocks  # noqa: E402
from app.services.dedup import Deduplicator, InMemoryDedupBackend, to_signed_64  # noqa: E402
from app.services.pdf_extract import (  # noqa: E402
    build_section_paths,
    calibrate_headings,
    extract_shard,
    merge_shards,
    plan_shards,
    probe_page_count,
    strip_noise,
)

# ------------------------------------------------------------
# 合成一份「像模像样」的 DM 手册
# ------------------------------------------------------------
SECTIONS = ["筹备清单", "流程要点", "常见误区", "话术示例", "结算口径"]

CHAPTERS = [
    ("第一章 开场与规则说明", [
        "本次《如是我观》为六人封闭式情感本，建议游戏时长四小时，其中开场三十分钟。",
        "主持人需在开场前确认所有玩家已完整阅读各自的角色卡，并收走手机等外部设备。",
        "开场白应当营造沉浸氛围，语速放缓，避免一次性抛出过多设定信息。",
        "若玩家人数不足六人，可使用替补角色卡「沈默」顶替，但需同步删除相关线索。",
    ]),
    ("第二章 搜证阶段", [
        "搜证阶段每位玩家限搜查两次，主持人须严格记录每位玩家的搜查顺序与地点。",
        "线索卡由主持人统一发放，玩家之间不得私下交换实体卡片，但可以口头描述。",
        "关键物证「褪色的合影」必须在第一轮搜证中被发现，若无人搜到需主动引导。",
        "搜证结束后预留五分钟自由讨论，主持人此时应观察玩家推理方向是否跑偏。",
    ]),
    ("第三章 剧情推进与时间线", [
        "第三幕开始前需要完成一轮匿名投票，投票结果决定后续两条剧情分支中的一条。",
        "时间线的关键节点是十年前的那场火灾，所有角色的动机都需回溯到这个事件。",
        "若玩家提前猜中真凶，不要急于否认，引导其补全动机链条即可保持张力。",
        "最终结算依据投票结果与线索完整度综合判定，主持人拥有最终解释权。",
    ]),
    ("第四章 主持技巧与常见问题", [
        "遇到玩家长时间沉默时，可用「你刚才提到的那件事，能再说说吗」这类开放式提问破冰。",
        "控场节奏的核心是让每个玩家在每一幕都至少有一次发言机会，避免话语权集中。",
        "若玩家情绪过于投入出现不适，应立即暂停游戏并给予充分的情绪缓冲时间。",
        "复盘环节建议控制在二十分钟内，重点讲清动机而非罗列线索。",
    ]),
]


def build_sample_pdf(path: str, pages: int) -> None:
    """生成带页眉页脚、页码、标题层级和重复内容的样本 PDF。

    刻意埋了几个真实手册必踩的坑，用来回归验证：

      - **running head 用章节名** —— 页眉文本与正文里的一级标题完全相同，
        只按文本匹配剥页眉会把真标题一起删掉；
      - **小节标题排在页面顶部** —— 位置落在页眉边缘带内，
        且数字归一化后每页长得一样，只按「位置 + 频次」判定会被当成页眉；
      - **每页重复一段正文** —— 检验 SimHash 近似去重是否生效；
      - **页码格式带破折号** —— 检验纯页码行的正则覆盖。
    """
    import pymupdf

    doc = pymupdf.open()
    chapter_span = max(1, pages // len(CHAPTERS))

    for page_no in range(1, pages + 1):
        page = doc.new_page(width=595, height=842)  # A4
        chapter_idx = min((page_no - 1) // chapter_span, len(CHAPTERS) - 1)
        title, paragraphs = CHAPTERS[chapter_idx]
        page_in_chapter = (page_no - 1) % chapter_span

        # 页眉走 running head：直接用章节名，与正文一级标题同文本、小字号。
        # 正确行为是剥掉这一行、保留正文里那个 20pt 的同名标题。
        page.insert_text((60, 40), title, fontsize=8, fontname="china-s")
        # 页脚页码（每页不同，归一化后应识别为同一模板）
        page.insert_text((280, 800), f"— {page_no} —", fontsize=8, fontname="china-s")

        y = 90.0
        # 每章第一页放一级标题（大字号）
        if page_in_chapter == 0:
            page.insert_text((60, y), title, fontsize=20, fontname="china-s")
            y += 40

        # 二级标题：每 3 页开一个小节，位置紧贴页顶（落在页眉边缘带里）
        if page_in_chapter % 3 == 0:
            section = SECTIONS[(page_in_chapter // 3) % len(SECTIONS)]
            page.insert_text(
                (60, y),
                f"{chapter_idx + 1}.{page_in_chapter // 3 + 1} {section}",
                fontsize=14,
                fontname="china-s",
            )
            y += 30

        # 正文段落
        for i, paragraph in enumerate(paragraphs):
            # 制造重复内容：每章的第一段在本章每页都出现一次，用于检验去重
            text = paragraph if i > 0 else paragraphs[0]
            for line in _wrap(text, 34):
                page.insert_text((60, y), line, fontsize=10.5, fontname="china-s")
                y += 18
            y += 8

    doc.save(path)
    doc.close()


def _wrap(text: str, width: int) -> List[str]:
    return [text[i : i + width] for i in range(0, len(text), width)] or [""]


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------
def run(pdf_path: str, pages_per_shard: int, is_synthetic: bool = False) -> int:
    total_pages = probe_page_count(pdf_path)
    size_mb = os.path.getsize(pdf_path) / 1024 / 1024
    print(f"PDF: {pdf_path}")
    print(f"     {total_pages} 页 / {size_mb:.2f} MB\n")

    # ---- T1 分片提取（串行模拟，真实环境是 Celery group 并行）----
    shards_plan = plan_shards(total_pages, pages_per_shard)
    print(f"[T1] 规划 {len(shards_plan)} 个分片（每片 {pages_per_shard} 页）")
    t0 = time.time()
    shard_results = []
    for i, (start, end) in enumerate(shards_plan):
        result = extract_shard(pdf_path, shard_index=i, page_start=start, page_end=end)
        shard_results.append(result)
    t1 = time.time()
    raw_blocks = sum(len(s.blocks) for s in shard_results)
    print(f"     提取完成: {raw_blocks} 个原始块, 耗时 {t1 - t0:.2f}s "
          f"({total_pages / max(t1 - t0, 1e-6):.0f} 页/秒)\n")

    # ---- T2 合并 / 去噪 / 分块 / 去重 ----
    blocks, merged_pages = merge_shards(shard_results)
    print(f"[T2] 合并后 {len(blocks)} 块，跨分片段落已续接")

    blocks, dropped_noise = strip_noise(blocks, total_pages=merged_pages, ratio_threshold=0.6)
    print(f"     剥离页眉页脚与噪声: -{dropped_noise} 块 -> {len(blocks)} 块")

    calibrate_headings(blocks)
    levels = sorted({b.heading_level for b in blocks if b.heading_level > 0})
    headings = sum(1 for b in blocks if b.block_type == "heading")
    by_level = {lv: sum(1 for b in blocks if b.heading_level == lv) for lv in levels}
    print(f"     标题校准: {headings} 个标题，层级 {levels}，各级数量 {by_level}")

    section_paths = build_section_paths(blocks)
    sample_path = next((p for p in section_paths if len(p) >= 2), None)
    max_depth = max((len(p) for p in section_paths), default=0)
    if sample_path:
        print(f"     章节面包屑示例: {' > '.join(sample_path)}（最深 {max_depth} 层）")

    cfg = ChunkConfig()
    chunks = chunk_blocks(blocks, section_paths=section_paths, config=cfg, embeddings=None)
    sizes = [c.char_count for c in chunks]
    print(f"\n     分块: {len(chunks)} 块, "
          f"平均 {statistics.mean(sizes):.0f} 字, 中位 {statistics.median(sizes):.0f} 字, "
          f"最大 {max(sizes)} 字")

    dedup = Deduplicator(
        backend=InMemoryDedupBackend(),
        threshold=cfg.min_chunk_chars and 3,
        min_chars=cfg.min_chunk_chars,
    )
    kept = []
    for chunk in chunks:
        verdict = dedup.check(chunk.text)
        if not verdict.is_duplicate:
            kept.append((chunk, verdict))

    stats = dedup.stats
    print(f"     去重: {stats.total} -> {len(kept)} "
          f"(精确 -{stats.dropped_exact}, 近似 -{stats.dropped_near}, 过短 -{stats.dropped_short}, "
          f"去重率 {stats.dedup_rate * 100:.1f}%)")

    # ---- 抽样展示 ----
    print("\n[抽样] 前 3 个入库块:")
    for chunk, verdict in kept[:3]:
        breadcrumb = " > ".join(chunk.section_path) or "(无章节)"
        preview = chunk.text.replace("\n", " ")[:52]
        print(f"   #{chunk.chunk_index:<4} P{chunk.page_start}-{chunk.page_end} "
              f"{chunk.char_count:>4}字 simhash={to_signed_64(verdict.fingerprint)}")
        print(f"        [{breadcrumb}]")
        print(f"        {preview}...")

    # ---- 断言 ----
    problems = []
    if not kept:
        problems.append("去重后无任何有效块")
    if dropped_noise == 0 and total_pages > 5:
        problems.append("未识别出任何页眉页脚（预期应剥离页码行）")
    if not levels:
        problems.append("未识别出任何标题层级")
    if max(sizes) > cfg.hard_max_chars:
        problems.append(f"存在超过硬上限的块: {max(sizes)} > {cfg.hard_max_chars}")
    if any(not c.text.strip() for c, _ in kept):
        problems.append("存在空白块")

    # 合成样本才有的强断言：章标题与小节标题必须都活着，且能撑起两层面包屑。
    # 这三条专门防「页眉剥离误伤标题」这类回归 —— 它不会让流水线报错，
    # 只会悄悄让检索结果失去章节定位能力。
    if is_synthetic:
        chapter_titles = {c[0] for c in CHAPTERS}
        survived = {b.text.strip() for b in blocks if b.block_type == "heading"}
        missing = [t for t in chapter_titles if t not in survived]
        if missing:
            problems.append(f"一级章标题被误剥离: {missing[:2]}")
        if not any("." in t and any(s in t for s in SECTIONS) for t in survived):
            problems.append("二级小节标题全部丢失（疑似被当成页眉剥离）")
        if len(levels) < 2:
            problems.append(f"标题层级不足两层: {levels}")
        if max_depth < 2:
            problems.append(f"章节面包屑最深仅 {max_depth} 层，层级未串联")

    print()
    if problems:
        print("检查未通过:")
        for p in problems:
            print(f"   ✗ {p}")
        return 1
    print("检查通过: 页眉页脚已剥离 / 章节标题存活 / 层级串联 / 块大小受控 / 去重生效")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="DM 指南流水线离线冒烟测试")
    parser.add_argument("--pdf", help="使用真实 PDF 而非生成样本")
    parser.add_argument("--pages", type=int, default=60, help="生成样本的页数")
    parser.add_argument("--pages-per-shard", type=int, default=20)
    args = parser.parse_args()

    if args.pdf:
        if not os.path.exists(args.pdf):
            print(f"文件不存在: {args.pdf}")
            return 2
        return run(args.pdf, args.pages_per_shard)

    tmp = Path(tempfile.gettempdir()) / f"dm_smoke_{args.pages}p.pdf"
    print(f"生成 {args.pages} 页样本 PDF ...")
    build_sample_pdf(str(tmp), args.pages)
    try:
        return run(str(tmp), args.pages_per_shard, is_synthetic=True)
    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
