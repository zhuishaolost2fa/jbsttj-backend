"""DM guide ask request validation tests.

可直接运行：python tests/test_dm_guide_ask_request.py
也兼容 pytest：pytest tests/test_dm_guide_ask_request.py
"""

import os
import sys

from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.schemas.dm_guide import AskRequest
from app.services.dm_service import _to_qa_hit


def test_ask_request_rejects_blank_question():
    try:
        AskRequest(question="           ", mode="hybrid", topK=6)
    except ValidationError:
        return
    raise AssertionError("blank question should be rejected")


def test_ask_request_strips_question():
    payload = AskRequest(question="  搜证阶段每人能搜几次？  ")
    assert payload.question == "搜证阶段每人能搜几次？"


def test_ask_request_accepts_chinese_alias():
    # 前端用「询问」键名传入，等价于 question
    payload = AskRequest(**{"询问": "搜证阶段每人能搜几次？"})
    assert payload.question == "搜证阶段每人能搜几次？"


def test_ask_request_code_field():
    payload = AskRequest(question="搜证阶段每人能搜几次？", code="xiaochikuaican")
    assert payload.code == "xiaochikuaican"


def test_ask_request_code_optional():
    # 路径式接口不传 code，应为 None
    payload = AskRequest(question="搜证阶段每人能搜几次？")
    assert payload.code is None


def test_qa_hit_carries_page_and_section():
    """回归测试：问答对必须带上章节与页码，否则 ask() 拼 AskSource 取
    h.section_path / h.page_start / h.page_end 会抛 AttributeError → 500。

    模拟 match_dm_qa RPC 的返回行（含 section_path / page_start / page_end）。
    """
    row = {
        "id": "qa-1",
        "document_id": "doc-1",
        "question": "马踏春是怎么死的",
        "answer": "被……",
        "category": "clue",
        "chunk_id": "c-1",
        "section_path": ["第一章", "真相"],
        "page_start": 12,
        "page_end": 13,
        "similarity": 0.9123,
    }
    hit = _to_qa_hit(row)
    assert hit.section_path == ["第一章", "真相"]
    assert hit.page_start == 12
    assert hit.page_end == 13
    # 复现 ask() 里从 QAHit 取字段组装 AskSource 的操作，确认不再 AttributeError
    from app.schemas.dm_guide import AskSource

    AskSource(
        type="qa",
        similarity=hit.similarity,
        question=hit.question,
        answer=hit.answer,
        section_path=hit.section_path,
        page_start=hit.page_start,
        page_end=hit.page_end,
    )


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
