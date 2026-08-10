"""ingest 任务 stage_detail 累计口径单测。

修复前：embed_and_store 每批上报「已向量化 {本批} 块 / {本批} 问答」，
多批并行时文案被后完成的批次覆盖、且永远是单批小数字，前端看起来
「到了 x 块 / Y 问答就再也不动」。修复后应为累计口径：
「已向量化 {库内实际累计}/{总块数} 块 / {累计问答} 问答」。

可直接运行：python tests/test_ingest_stage_detail.py
也兼容 pytest：pytest tests/test_ingest_stage_detail.py
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.tasks.dm_ingest as m


def _chunk(i):
    return {
        "text": f"第 {i} 块正文内容",
        "page_start": i,
        "page_end": i,
        "section_path": [],
        "block_type": "body",
        "chunk_index": i,
        "content_hash": f"hash{i}",
    }


def _fake_store(**overrides):
    store = mock.Mock()
    store.insert_chunks.side_effect = lambda rows: [
        {"id": f"c{r['content_hash']}", "content_hash": r["content_hash"]}
        for r in rows
    ]
    store.count_chunks.return_value = overrides.get("done_chunks", 12)
    store.count_qa.return_value = overrides.get("done_qa", 20)
    store.bump_job.return_value = overrides.get("bump_return", None)
    return store


def _fake_llm():
    client = mock.Mock()
    client.embed_documents.side_effect = lambda texts: [[0.1, 0.2]] * len(texts)
    return client


def test_embed_stage_detail_is_cumulative():
    """stage_detail 必须用累计口径（库内实际行数 / 总块数），不是本批数量。"""
    store = _fake_store(done_chunks=12, done_qa=20)
    payload = {
        "chunks": [_chunk(0), _chunk(1)],
        "qa": [
            {"question": "问题一？", "answer": "答案一", "source_index": 0,
             "category": "general"},
        ],
    }
    with mock.patch.object(m, "get_dm_store", return_value=store), \
         mock.patch.object(m, "get_llm_client", return_value=_fake_llm()):
        m.embed_and_store(
            payload, job_id="j1", document_id="d1", script_id="s1",
            total_chunks=24,
        )

    assert store.bump_job.call_count == 1
    kwargs = store.bump_job.call_args.kwargs
    assert kwargs["status"] == m.JOB_EMBEDDING
    # 本批只有 2 块 / 1 问答，但文案必须展示累计 12/24 块 / 20 问答
    assert kwargs["stage_detail"] == "已向量化 12/24 块 / 20 问答", kwargs["stage_detail"]
    # 计数增量仍走 +=（防并发丢更新），语义不变
    assert kwargs["embedded_chunks"] == 2
    assert kwargs["embedded_qa"] == 1


def test_embed_stage_detail_without_total():
    """老任务没传 total_chunks 时降级为只显示累计量，不报错。"""
    store = _fake_store(done_chunks=7, done_qa=9)
    payload = {"chunks": [_chunk(0)], "qa": []}
    with mock.patch.object(m, "get_dm_store", return_value=store), \
         mock.patch.object(m, "get_llm_client", return_value=_fake_llm()):
        m.embed_and_store(payload, job_id="j1", document_id="d1", script_id="s1")

    kwargs = store.bump_job.call_args.kwargs
    assert kwargs["stage_detail"] == "已向量化 7 块 / 9 问答", kwargs["stage_detail"]


def test_generate_qa_reports_cumulative_progress():
    """T3 生成阶段也要上报累计进度，否则慢速阶段前端看不到任何动静。"""
    store = _fake_store(bump_return={"total_qa": 42})
    qa_pair = mock.Mock()
    qa_pair.to_dict.return_value = {"question": "问？", "answer": "答"}
    llm = mock.Mock()
    llm.generate_qa.return_value = [qa_pair]

    with mock.patch.object(m, "get_dm_store", return_value=store), \
         mock.patch.object(m, "get_llm_client", return_value=llm):
        m.generate_qa([_chunk(0)], job_id="j1", document_id="d1", script_id="s1")

    assert store.bump_job.call_count == 2
    first, second = store.bump_job.call_args_list
    assert first.kwargs["total_qa"] == 1  # 本批增量
    assert second.kwargs["stage_detail"] == "问答对生成中：已累计 42 条", \
        second.kwargs["stage_detail"]


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
