"""QA 标题链（按标题分组的 QA 树）回归测试。

覆盖：
  1. 多个 QA 聚合到同一标题下，行文顺序保持；
  2. 嵌套章节路径建成多级树，中间节点被正确补出；
  3. 无章节信息的 QA 归入「未分节」；
  4. section_path 历史脏数据（逗号串）兼容；
  5. 服务层按 script_code 透传并汇总计数。

可直接运行：python tests/test_qa_title_chain.py
也兼容 pytest：pytest tests/test_qa_title_chain.py
"""

import asyncio
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.dm_service import DMGuideService, _build_title_tree


def _row(path, qid, question, title="", answer="答", category="rule", page=1):
    return {
        "section_path": path,
        "title": title,
        "qa_id": qid,
        "question": question,
        "answer": answer,
        "category": category,
        "page_start": page,
        "page_end": page,
    }


def test_multiple_qa_grouped_under_same_title_in_order():
    rows = [
        _row(["第一幕", "搜证规则"], "q1", "每人限搜几次？"),
        _row(["第一幕", "搜证规则"], "q2", "可以隐瞒线索吗？"),
        _row(["第一幕", "搜证规则"], "q3", "秘密线索怎么拿？"),
    ]
    titles, leaf_titles, total_qa = _build_title_tree(rows)

    assert total_qa == 3 and leaf_titles == 1
    assert len(titles) == 1 and titles[0].title == "第一幕"
    child = titles[0].children[0]
    assert child.title == "搜证规则"
    assert child.path == ["第一幕", "搜证规则"]
    assert child.qa_count == 3
    assert [q.question for q in child.qa] == [
        "每人限搜几次？", "可以隐瞒线索吗？", "秘密线索怎么拿？",
    ]
    # 父节点本身不直接挂 QA
    assert titles[0].qa_count == 0


def test_nested_paths_build_intermediate_nodes():
    rows = [
        _row(["第二章", "2.1 时间线"], "q1", "死者死亡时间？"),
        _row(["第二章", "2.2 不在场证明", "2.2.1 张三"], "q2", "张三在哪？"),
        _row(["第三章"], "q3", "如何结案？"),
    ]
    titles, leaf_titles, total_qa = _build_title_tree(rows)

    assert total_qa == 3 and leaf_titles == 3
    assert [t.title for t in titles] == ["第二章", "第三章"]
    ch2 = titles[0].children
    assert [c.title for c in ch2] == ["2.1 时间线", "2.2 不在场证明"]
    assert ch2[1].children[0].title == "2.2.1 张三"
    assert ch2[1].children[0].qa_count == 1
    # 「第三章」直接挂在根级节点上
    assert titles[1].qa_count == 1


def test_unsectioned_qa_falls_into_untitled_node():
    rows = [
        _row([], "q1", "开场白怎么说？"),
        _row(None, "q2", "道具清单？"),
    ]
    titles, leaf_titles, total_qa = _build_title_tree(rows)

    assert total_qa == 2 and leaf_titles == 1
    assert titles[0].title == "未分节"
    assert titles[0].qa_count == 2


def test_legacy_comma_string_section_path_compat():
    rows = [_row("第一幕,搜证规则", "q1", "每人限搜几次？")]
    titles, _, _ = _build_title_tree(rows)
    assert titles[0].title == "第一幕"
    assert titles[0].children[0].title == "搜证规则"


def test_service_passes_code_and_aggregates_counts():
    store = mock.Mock()
    store.list_qa_titles.return_value = [
        _row(["第一幕"], "q1", "问题一"),
        _row(["第一幕"], "q2", "问题二"),
    ]
    svc = DMGuideService()
    svc._settings = mock.Mock()
    with mock.patch("app.services.dm_service.store_mod.get_dm_store", return_value=store):
        out = asyncio.run(
            svc.qa_title_chain(script_code="Liu-Jiao-Guan", script_title="六角馆谋杀奇谋")
        )

    store.list_qa_titles.assert_called_once_with("liu-jiao-guan")  # 统一小写
    assert out.script_code == "liu-jiao-guan"
    assert out.script_title == "六角馆谋杀奇谋"
    assert out.total_qa == 2 and out.total_titles == 1
    assert out.titles[0].qa_count == 2
    # 序列化走 camelCase 别名
    dumped = out.model_dump(by_alias=True)
    assert "scriptCode" in dumped and "totalQa" in dumped
    assert "qaCount" in dumped["titles"][0]


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
