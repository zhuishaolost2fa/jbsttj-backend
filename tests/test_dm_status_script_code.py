"""DM 手册状态与检索按 script_code 聚合的回归测试。

可直接运行：python tests/test_dm_status_script_code.py
也兼容 pytest：pytest tests/test_dm_status_script_code.py
"""

import asyncio
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.dm_service import DMGuideService, script_dm_code


class _FakeScript:
    def __init__(self, sid, title, extra=None):
        self.id = sid
        self.title = title
        self.extra = extra


def _mock_settings():
    return mock.Mock(
        dm_rag_enabled=True,
        missing_rag_config=mock.Mock(return_value=[]),
        dm_search_top_k=8,
        dm_search_min_similarity=0.25,
        dm_search_qa_supplement_k=4,
        dm_search_qa_boost=1.0,
    )


async def _status_with_store(store, script):
    svc = DMGuideService()
    svc._settings = _mock_settings()
    with mock.patch("app.services.dm_service.store_mod.get_dm_store", return_value=store):
        return await svc.get_status(script)


async def _search_with_store(store):
    svc = DMGuideService()
    svc._settings = _mock_settings()
    client = mock.Mock()
    client.aembed_query = mock.AsyncMock(return_value=[0.1, 0.2, 0.3])
    with mock.patch("app.services.dm_service.store_mod.get_dm_store", return_value=store), \
         mock.patch("app.services.llm.get_llm_client", return_value=client):
        return await svc.search(
            query="主持人要查的规则",
            script_id="sid-1",
            script_code="wu-dou-yi-ying",
            mode="hybrid",
            top_k=4,
        )


def test_get_status_aggregates_documents_by_script_code():
    store = mock.Mock()
    store.list_active_documents_by_code.return_value = [
        {
            "id": "doc-new",
            "file_name": "new.pdf",
            "total_pages": 30,
            "total_chunks": 12,
            "total_qa": 5,
            "version": 2,
        },
        {
            "id": "doc-old",
            "file_name": "old.pdf",
            "total_pages": 20,
            "total_chunks": 8,
            "total_qa": 3,
            "version": 1,
        },
    ]
    store.get_active_document.return_value = None
    store.latest_job.return_value = {
        "id": "job-1",
        "script_id": "sid-1",
        "status": "completed",
        "document_id": "doc-new",
        "total_chunks": 12,
        "embedded_chunks": 12,
        "total_qa": 5,
        "embedded_qa": 5,
    }

    script = _FakeScript("sid-1", "雾都疑影")
    code = script_dm_code(script)
    out = asyncio.run(_status_with_store(store, script))

    assert out.document_id == "doc-new"
    assert out.file_name == "new.pdf"
    assert out.total_pages == 50
    assert out.total_chunks == 20
    assert out.total_qa == 8
    assert out.version == 2
    assert out.indexed is True
    store.list_active_documents_by_code.assert_called_once_with(code)
    store.get_active_document.assert_not_called()
    store.latest_job.assert_called_once_with("sid-1", script_code=code)


def test_get_status_falls_back_to_script_id_when_no_code_docs_exist():
    store = mock.Mock()
    store.list_active_documents_by_code.return_value = []
    store.get_active_document.return_value = {
        "id": "legacy-doc",
        "file_name": "legacy.pdf",
        "total_pages": 10,
        "total_chunks": 4,
        "total_qa": 1,
        "version": 7,
    }
    store.latest_job.return_value = None

    script = _FakeScript("sid-2", "雾都疑影")
    code = script_dm_code(script)
    out = asyncio.run(_status_with_store(store, script))

    assert out.document_id == "legacy-doc"
    assert out.file_name == "legacy.pdf"
    assert out.total_pages == 10
    assert out.total_chunks == 4
    assert out.total_qa == 1
    assert out.version == 7
    assert out.indexed is True
    store.list_active_documents_by_code.assert_called_once_with(code)
    store.get_active_document.assert_called_once_with("sid-2", script_code=code)


def test_search_forwards_script_code_to_retrieval():
    store = mock.Mock()
    store.match_chunks.return_value = []
    store.match_qa.return_value = []

    out = asyncio.run(_search_with_store(store))

    assert out.query == "主持人要查的规则"
    store.match_chunks.assert_called_once()
    chunk_call = store.match_chunks.call_args.kwargs
    assert chunk_call["script_id"] == "sid-1"
    assert chunk_call["script_code"] == "wu-dou-yi-ying"
    store.match_qa.assert_called_once()
    qa_call = store.match_qa.call_args.kwargs
    assert qa_call["script_id"] == "sid-1"
    assert qa_call["script_code"] == "wu-dou-yi-ying"


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
