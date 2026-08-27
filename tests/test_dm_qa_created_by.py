"""回归测试：入库流水线必须把「触发上传的操作用户」写入每条 QA 对。

每条由手册解析产出的问答对都应在数据库里有 created_by 字段，
记录是谁上传的手册产出了它（与 script_dm_jobs.created_by 同源）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock

import app.tasks.dm_ingest as dm_ingest


def test_embed_and_store_writes_created_by_to_qa():
    captured = {}

    class FakeStore:
        def insert_chunks(self, rows):
            captured["chunks"] = rows
            return [
                {**r, "id": f"chunk-{i}"} for i, r in enumerate(rows)
            ]

        def insert_qa(self, rows):
            captured["qa"] = rows
            return list(rows)

        def count_chunks(self, document_id):
            return len(captured.get("chunks", []))

        def count_qa(self, document_id):
            return len(captured.get("qa", []))

        def count_stories(self, document_id):
            return 0

        def update_job(self, job_id, patch):
            captured.setdefault("jobs", []).append(patch)

    class FakeLLM:
        def embed_documents(self, texts):
            # 每个文本返回一个 3 维假向量，足以驱动入库逻辑
            return [[0.1, 0.2, 0.3] for _ in texts]

    fake_store = FakeStore()
    fake_llm = FakeLLM()

    with mock.patch.object(dm_ingest, "get_dm_store", return_value=fake_store), \
         mock.patch.object(dm_ingest, "get_llm_client", return_value=fake_llm):

        dm_ingest.embed_and_store(
            {
                "chunks": [
                    {
                        "chunk_index": 0,
                        "content_hash": "h0",
                        "text": "正文",
                        "page_start": 1,
                        "page_end": 1,
                        "section_path": ["第一章"],
                        "block_type": "body",
                        "char_count": 2,
                    }
                ],
                "qa": [
                    {
                        "question": "马踏春是怎么死的",
                        "answer": "被……",
                        "category": "clue",
                        "source_index": 0,
                    }
                ],
            },
            job_id="job-1",
            document_id="doc-1",
            script_id="script-1",
            script_code="shan-gui-mu",
            total_chunks=1,
            created_by="user-abc-123",
        )

    qa_rows = captured.get("qa")
    assert qa_rows, "QA 行未被写入"
    assert len(qa_rows) == 1, f"预期 1 条 QA，实际 {len(qa_rows)}"
    assert qa_rows[0].get("created_by") == "user-abc-123", (
        f"QA 行的 created_by 应为 user-abc-123，实际 {qa_rows[0].get('created_by')!r}"
    )
    print("PASS: QA 行携带 created_by =", qa_rows[0]["created_by"])


def test_embed_and_store_null_created_by_when_missing():
    """未传 created_by 时，写入 NULL 而非报异常。"""
    captured = {}

    class FakeStore:
        def insert_chunks(self, rows):
            captured["chunks"] = rows
            return [{**r, "id": "c0"} for r in rows]

        def insert_qa(self, rows):
            captured["qa"] = rows
            return list(rows)

        def count_chunks(self, document_id):
            return 1

        def count_qa(self, document_id):
            return len(captured.get("qa", []))

        def count_stories(self, document_id):
            return 0

        def update_job(self, job_id, patch):
            pass

    class FakeLLM:
        def embed_documents(self, texts):
            return [[0.1, 0.2, 0.3] for _ in texts]

    with mock.patch.object(dm_ingest, "get_dm_store", return_value=FakeStore()), \
         mock.patch.object(dm_ingest, "get_llm_client", return_value=FakeLLM()):
        dm_ingest.embed_and_store(
            {
                "chunks": [
                    {
                        "chunk_index": 0,
                        "content_hash": "h0",
                        "text": "正文",
                        "page_start": 1,
                        "page_end": 1,
                        "section_path": [],
                        "block_type": "body",
                        "char_count": 2,
                    }
                ],
                "qa": [{"question": "谁杀了人", "answer": "不知道", "category": "other", "source_index": 0}],
            },
            job_id="job-2",
            document_id="doc-2",
            script_id="script-2",
            script_code="x",
            total_chunks=1,
            # 不传 created_by
        )

    qa_rows = captured.get("qa")
    assert qa_rows[0].get("created_by") is None, "缺省 created_by 应为 None"
    print("PASS: 缺省 created_by = None")


if __name__ == "__main__":
    test_embed_and_store_writes_created_by_to_qa()
    test_embed_and_store_null_created_by_when_missing()
    print("ALL PASS")
