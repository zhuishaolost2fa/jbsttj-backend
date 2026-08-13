"""chunk_and_dedup 每轮必须清空文档级去重指纹命名空间的回归测试。

事故背景（山鬼母手册）：force 重跑只清了数据库（purge_document），
Redis 里的去重指纹带 7 天 TTL 仍然存活。同一份手册重传时，
所有 chunk 被判为 exact 重复 → 0 块入库 → 前端永远停在「已解析、
不可问答」（parsed）状态。T2 自身重试也有同样问题。

修复：T2 开始前对 backend 调一次 clear()。本测试用一个跨调用共享的
有状态后端模拟 Redis，连续跑两轮 chunk_and_dedup，断言第二轮依然
保留全部 chunk（修复前第二轮 kept=0）。

可直接运行：python tests/test_dedup_namespace_reset.py
也兼容 pytest：pytest tests/test_dedup_namespace_reset.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.tasks.dm_ingest as m
from app.services.dedup import InMemoryDedupBackend


class _SharedBackend(InMemoryDedupBackend):
    """跨「任务轮次」共享状态的后端，模拟 Redis 的持久指纹集合。"""

    def __init__(self):
        super().__init__()
        self.clear_calls = 0

    def clear(self):
        self.clear_calls += 1
        super().clear()


_TEXTS = [
    "第一幕搜证阶段每位玩家限搜三次线索，搜证顺序按座位顺时针进行。",
    "凶手使用的毒药来自书房抽屉里的蓝色玻璃瓶，标签已被撕掉。",
    "结局复盘时请主持人先播放音频，再依次公布每位角色的隐藏任务。",
]


def _chunk_obj(i):
    chunk = mock.Mock()
    chunk.text = _TEXTS[i]
    chunk.to_dict.return_value = {
        "text": chunk.text,
        "page_start": i,
        "page_end": i,
        "section_path": [],
        "block_type": "body",
        "chunk_index": i,
    }
    return chunk


def _run_once(store, backend, settings):
    shard_payload = {
        "shard_index": 0, "page_start": 1, "page_end": 6,
        "page_count": 6, "blocks": [], "font_sizes": [],
    }
    chunks = [_chunk_obj(i) for i in range(3)]
    with mock.patch.object(m, "get_dm_store", return_value=store), \
         mock.patch.object(m, "get_settings", return_value=settings), \
         mock.patch.object(m, "build_backend", return_value=backend), \
         mock.patch.object(m, "merge_shards", return_value=([], 6)), \
         mock.patch.object(m, "strip_noise", side_effect=lambda b, **kw: (b, 0)), \
         mock.patch.object(m, "calibrate_headings", side_effect=lambda b: None), \
         mock.patch.object(m, "build_section_paths", return_value=[]), \
         mock.patch.object(m, "chunk_blocks", return_value=chunks), \
         mock.patch.object(m, "celery_available", return_value=False):
        return m.chunk_and_dedup(
            [shard_payload],
            job_id="j1", document_id="d1", script_id="s1",
        )


def _fake_settings():
    s = mock.Mock()
    s.siliconflow_api_key = ""  # 不构造 embeddings，走纯递归切分支
    s.dm_simhash_threshold = 3
    s.dm_min_chunk_chars = 10
    s.dm_header_footer_ratio = 0.5
    s.dm_qa_batch_size = 6
    return s


def test_second_run_clears_stale_fingerprints():
    """模拟 force 重跑：共享后端里已有首轮指纹，第二轮不得全部误杀。"""
    store = mock.Mock()
    backend = _SharedBackend()
    settings = _fake_settings()

    first = _run_once(store, backend, settings)
    assert first["chunks"] == 3, first

    # 修复前：第二轮 3 块全部被首轮残留指纹判为 exact 重复，kept=0
    second = _run_once(store, backend, settings)
    assert second["chunks"] == 3, (
        f"force 重跑后 kept={second['chunks']}，历史去重指纹未被清空"
    )
    assert backend.clear_calls == 2, backend.clear_calls


def test_clear_failure_does_not_break_pipeline():
    """clear() 抛异常时流水线仍应继续（仅告警），不能中断解析。"""
    store = mock.Mock()
    backend = _SharedBackend()
    backend.clear = mock.Mock(side_effect=RuntimeError("redis down"))
    settings = _fake_settings()

    result = _run_once(store, backend, settings)
    assert result["chunks"] == 3, result


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
