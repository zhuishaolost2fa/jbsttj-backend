"""DM guide ask request validation tests.

可直接运行：python tests/test_dm_guide_ask_request.py
也兼容 pytest：pytest tests/test_dm_guide_ask_request.py
"""

import os
import sys

from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.schemas.dm_guide import AskRequest


def test_ask_request_rejects_blank_question():
    try:
        AskRequest(question="           ", mode="hybrid", topK=6)
    except ValidationError:
        return
    raise AssertionError("blank question should be rejected")


def test_ask_request_strips_question():
    payload = AskRequest(question="  搜证阶段每人能搜几次？  ")
    assert payload.question == "搜证阶段每人能搜几次？"


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
