"""用户提问沉淀（低相似度问题 → 真人解答 → 引导问题）的单元测试。

可直接运行：python tests/test_dm_questions.py
也兼容 pytest：pytest tests/test_dm_questions.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.schemas.dm_guide import (
    AnswerQuestionRequest,
    AskResponse,
    GuideQuestions,
    QuestionRecord,
)
from app.services.dm_service import _merge_profiles, _to_question_record


def _row(**over):
    base = {
        "id": "q-1",
        "script_id": "s-1",
        "script_code": "liujiaoguan",
        "question": "搜证阶段每人能搜几次？",
        "ask_count": 5,
        "best_similarity": 0.3123,
        "status": "answered",
        "answer": "每人限搜 3 次",
        "answered_by": "u-9",
        "answered_at": None,
        "created_by": "u-1",
        "created_at": None,
        "updated_at": None,
    }
    base.update(over)
    return base


def _profiles():
    """模拟批量查 profiles 的返回：提问者有真实头像，解答者用渐变头像。"""
    return {
        "u-1": {
            "id": "u-1",
            "nickname": "小侦探",
            "avatar_url": "https://cdn.example.com/avatars/u-1.png",
            "avatar_color": None,
        },
        "u-9": {
            "id": "u-9",
            "nickname": "金牌DM",
            "avatar_url": None,
            "avatar_color": 3,
        },
    }


def test_question_record_from_row():
    rec = _to_question_record(_row())
    assert rec.id == "q-1"
    assert rec.script_code == "liujiaoguan"
    assert rec.ask_count == 5
    assert rec.status == "answered"
    assert rec.answer == "每人限搜 3 次"
    # best_similarity 保留 4 位小数
    assert rec.best_similarity == 0.3123


def test_merge_profiles_fills_display_fields():
    """读取时按 user id 批量合并 profiles：昵称/头像透出，且走 camelCase 别名。"""
    rec = _to_question_record(_row())
    _merge_profiles([rec], _profiles())
    assert rec.created_by_nickname == "小侦探"
    assert rec.created_by_avatar_url == "https://cdn.example.com/avatars/u-1.png"
    assert rec.created_by_avatar_color is None
    assert rec.answered_by_nickname == "金牌DM"
    assert rec.answered_by_avatar_url is None
    assert rec.answered_by_avatar_color == 3

    dumped = rec.model_dump(by_alias=True)
    assert dumped["createdByNickname"] == "小侦探"
    assert dumped["answeredByAvatarColor"] == 3
    assert "answeredByAvatarUrl" in dumped


def test_merge_profiles_leaves_unknown_users_empty():
    """profiles 里查不到的用户（注销/脏数据）保持 None，不得抛异常。"""
    rec = _to_question_record(_row())
    _merge_profiles([rec], {})  # 一个资料都没查到
    assert rec.created_by_nickname is None
    assert rec.answered_by_avatar_color is None


def test_question_record_profile_fields_default_none():
    """未合并 profiles 时展示字段为 None，前端按渐变头像兜底。"""
    rec = _to_question_record({"id": "q-3", "script_id": "s-1", "question": "?"})
    assert rec.created_by_avatar_url is None
    assert rec.answered_by_avatar_color is None
    assert rec.answered_by_nickname is None


def test_question_record_defaults_for_sparse_row():
    """历史脏数据缺字段时不得抛异常，走默认值。"""
    rec = _to_question_record({"id": "q-2", "script_id": "s-1", "question": "?"})
    assert rec.ask_count == 1
    assert rec.status == "pending"
    assert rec.answer is None
    assert rec.best_similarity == 0.0


def test_answer_request_strips_answer():
    payload = AnswerQuestionRequest(answer="  每人限搜 3 次  ")
    assert payload.answer == "每人限搜 3 次"


def test_answer_request_rejects_blank():
    from pydantic import ValidationError

    try:
        AnswerQuestionRequest(answer="    ")
    except ValidationError:
        return
    raise AssertionError("blank answer should be rejected")


def test_ask_response_carries_low_similarity_flags():
    """ask 响应新增 bestSimilarity / needHumanAnswer，且走 camelCase 别名输出。"""
    resp = AskResponse(
        question="搜证阶段每人能搜几次？",
        answer="手册中未找到相关内容",
        mode="hybrid",
        best_similarity=0.21,
        need_human_answer=True,
    )
    dumped = resp.model_dump(by_alias=True)
    assert dumped["bestSimilarity"] == 0.21
    assert dumped["needHumanAnswer"] is True


def test_ask_response_flags_default_off():
    resp = AskResponse(question="q", answer="a", mode="qa")
    assert resp.best_similarity == 0.0
    assert resp.need_human_answer is False


def test_guide_questions_shape():
    g = GuideQuestions(
        script_code="liujiaoguan",
        script_title="六角馆谋杀奇谋",
        items=[_to_question_record(_row())],
    )
    dumped = g.model_dump(by_alias=True)
    assert dumped["scriptCode"] == "liujiaoguan"
    assert dumped["items"][0]["askCount"] == 5


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
