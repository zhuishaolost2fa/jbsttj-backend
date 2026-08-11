"""import-status 整体状态机单测。

覆盖关键路径与 0-chunk 完成的边界：
- 正常解析完成（indexed=True） -> overall_status == "ready"
- 0 chunk 的退化完成（job=completed 但 indexed=False） -> overall_status == "parsed"
  （修复前会落到 "pending"，与「未开始」混淆，前端无法感知解析结束）
- 解析进行中 / 失败 / 未传手册 等常规分支。

可直接运行：python tests/test_import_status.py
也兼容 pytest：pytest tests/test_import_status.py
"""
import asyncio
import os
import sys

# 让脚本能直接 import app（项目根在 tests 的父目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.dm_service import DMGuideService
from app.schemas.dm_guide import DMGuideStatus, JobProgress


class _FakeScript:
    def __init__(self, sid, title=None, extra=None):
        self.id = sid
        self.title = title
        self.extra = extra


def _status(has_guide, indexed, job=None, **kw):
    return DMGuideStatus(
        script_id=kw.get("script_id", "s1"),
        has_guide=has_guide,
        indexed=indexed,
        document_id=kw.get("document_id"),
        total_chunks=kw.get("total_chunks", 0),
        total_qa=kw.get("total_qa", 0),
        job=job,
    )


def _job(status, **kw):
    return JobProgress(
        job_id=kw.get("job_id", "j1"),
        script_id=kw.get("script_id", "s1"),
        document_id=kw.get("document_id", "d1"),
        status=status,
        total_chunks=kw.get("total_chunks", 0),
        embedded_chunks=kw.get("embedded_chunks", 0),
        total_qa=kw.get("total_qa", 0),
        embedded_qa=kw.get("embedded_qa", 0),
    )


async def _overall(has_guide, indexed, job=None, **kw):
    svc = DMGuideService()
    st = _status(has_guide, indexed, job=job, **kw)

    async def _fake_get_status(script):
        return st

    svc.get_status = _fake_get_status  # 只测整体判定，屏蔽 DB 依赖
    out = await svc.get_import_status(_FakeScript(st.script_id))
    return out.overall_status, out


def test_zero_chunk_completed_maps_to_parsed():
    overall, out = asyncio.run(
        _overall(has_guide=True, indexed=False, job=_job("completed"))
    )
    assert overall == "parsed", (overall, [p.status for p in out.phases])
    # parse 阶段应当标记为 done，ready 阶段仍为 pending（无可用索引）
    by_key = {p.key: p.status for p in out.phases}
    assert by_key["parse"] == "done"
    assert by_key["ready"] == "pending"


def test_normal_completed_maps_to_ready():
    overall, out = asyncio.run(
        _overall(
            has_guide=True,
            indexed=True,
            job=_job("completed", total_chunks=10, total_qa=3),
        )
    )
    assert overall == "ready", overall
    by_key = {p.key: p.status for p in out.phases}
    assert by_key["parse"] == "done"
    assert by_key["ready"] == "done"


def test_skipped_job_maps_to_parsed_when_not_indexed():
    overall, _ = asyncio.run(
        _overall(has_guide=True, indexed=False, job=_job("skipped"))
    )
    assert overall == "parsed", overall


def test_parsing_active():
    overall, _ = asyncio.run(
        _overall(
            has_guide=True,
            indexed=False,
            job=_job("embedding", total_chunks=10, embedded_chunks=5),
        )
    )
    assert overall == "parsing", overall


def test_no_guide():
    overall, _ = asyncio.run(_overall(has_guide=False, indexed=False, job=None))
    assert overall == "no_guide", overall


def test_parse_failed():
    overall, _ = asyncio.run(
        _overall(has_guide=True, indexed=False, job=_job("failed"))
    )
    assert overall == "failed", overall


def test_parse_cancelled_falls_to_pending():
    overall, _ = asyncio.run(
        _overall(has_guide=True, indexed=False, job=_job("cancelled"))
    )
    assert overall == "pending", overall


def test_embedding_at_cap_maps_to_done_and_ready():
    """向量化块到达上限（embedded==total）即使 job 仍 embedding，也算解析完成。"""
    overall, out = asyncio.run(
        _overall(
            has_guide=True,
            indexed=True,
            job=_job("embedding", total_chunks=10, embedded_chunks=10),
        )
    )
    assert overall == "ready", overall
    by_key = {p.key: (p.status, p.progress) for p in out.phases}
    assert by_key["parse"] == ("done", 100.0), by_key
    assert by_key["ready"] == ("done", 100.0), by_key


def test_embedding_overshoot_maps_to_done():
    """重试导致 embedded > total（+= 虚高）时同样判完成。"""
    overall, out = asyncio.run(
        _overall(
            has_guide=True,
            indexed=True,
            job=_job("embedding", total_chunks=10, embedded_chunks=12),
        )
    )
    assert overall == "ready", overall
    by_key = {p.key: p.status for p in out.phases}
    assert by_key["parse"] == "done"


def test_embedding_at_cap_without_index_maps_to_parsed():
    """到上限但尚未激活索引（indexed=False）时整体落 parsed，不谎称 ready。"""
    overall, out = asyncio.run(
        _overall(
            has_guide=True,
            indexed=False,
            job=_job("embedding", total_chunks=10, embedded_chunks=10),
        )
    )
    assert overall == "parsed", overall
    by_key = {p.key: p.status for p in out.phases}
    assert by_key["parse"] == "done"
    assert by_key["ready"] == "pending"


def test_embedding_below_cap_stays_parsing():
    """未到上限仍是 parsing。"""
    overall, out = asyncio.run(
        _overall(
            has_guide=True,
            indexed=True,
            job=_job("embedding", total_chunks=10, embedded_chunks=9),
        )
    )
    assert overall == "parsing", overall
    by_key = {p.key: p.status for p in out.phases}
    assert by_key["parse"] == "active"


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
