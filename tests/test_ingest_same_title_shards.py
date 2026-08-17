"""同名分片手册导入互不覆盖的回归测试。

场景：同一部剧本（如《六角馆谋杀奇谋》）被拆成两个同名文档，
前端创建两个同名剧本记录（不同 script_id、相同 script_code）分别导入。

期望：
  1. 分片 B 触发解析时，**不得**把分片 A 正在跑的任务误判为
     「本剧本文件已更换」而取消 —— 防重粒度是 script_id，不是 script_code；
  2. 同一剧本实例内「换了文件重传」仍然取消旧任务（原语义保留）；
  3. 同一剧本实例内「同文件重复触发」仍复用现有任务（原语义保留）。

可直接运行：python tests/test_ingest_same_title_shards.py
也兼容 pytest：pytest tests/test_ingest_same_title_shards.py
"""

import asyncio
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.dm_service import DMGuideService, script_dm_code


class _FakeScript:
    def __init__(self, sid, title, object_key):
        self.id = sid
        self.title = title
        self.extra = {"dmGuide": {"objectKey": object_key, "fileName": object_key.rsplit("/", 1)[-1]}}


def _mock_settings():
    return mock.Mock(
        dm_rag_enabled=True,
        missing_rag_config=mock.Mock(return_value=[]),
        dm_max_pdf_bytes=200 * 1024 * 1024,
    )


def _run_trigger(store, script, dispatched):
    svc = DMGuideService()
    svc._settings = _mock_settings()
    with mock.patch("app.services.dm_service.store_mod.get_dm_store", return_value=store), \
         mock.patch("app.tasks.dm_ingest.dispatch_pipeline",
                    side_effect=lambda **kw: dispatched.append(kw) or "task-1"):
        return asyncio.run(svc.trigger_ingest(script, user_id="u1"))


def _make_store(active_job=None):
    store = mock.Mock()
    store.find_active_job.return_value = active_job
    store.JOB_PENDING = "pending"
    return store


def test_shard_b_does_not_cancel_shard_a_running_job():
    """分片 A 的任务在跑（同 script_code、不同 script_id），分片 B 导入必须照常派发。"""
    script_a = _FakeScript("sid-A", "六角馆谋杀奇谋", "temp/u1/part1.docx")
    script_b = _FakeScript("sid-B", "六角馆谋杀奇谋", "temp/u1/part2.docx")
    assert script_dm_code(script_a) == script_dm_code(script_b)  # 前提：同名同 code

    # 库里「按 script_id 查」时，B 没有自己在跑的任务 —— A 的任务根本不该被看到
    store = _make_store(active_job=None)
    dispatched = []
    resp = _run_trigger(store, script_b, dispatched)

    assert resp.reused is False
    assert len(dispatched) == 1                      # B 的流水线正常派发
    store.find_active_job.assert_called_once_with("sid-B")   # ★ 只按 script_id 查
    store.update_job.assert_not_called()             # 没有取消任何人的任务


def test_same_script_replacing_file_still_cancels_old_job():
    """同一剧本实例换了文件重传：旧任务应被取消（原语义保留）。"""
    script = _FakeScript("sid-A", "六角馆谋杀奇谋", "temp/u1/new.docx")
    active = {"id": "job-old", "status": "extracting", "object_key": "temp/u1/old.pdf"}
    store = _make_store(active_job=active)
    dispatched = []
    resp = _run_trigger(store, script, dispatched)

    assert resp.reused is False
    assert len(dispatched) == 1
    # 旧任务被标记取消
    cancel_calls = [c for c in store.update_job.call_args_list
                    if c.args[0] == "job-old"]
    assert cancel_calls, "换文件重传应取消旧任务"


def test_same_script_same_file_reuses_running_job():
    """同一剧本实例同文件重复触发：复用现有任务，不重复派发。"""
    script = _FakeScript("sid-A", "六角馆谋杀奇谋", "temp/u1/part1.docx")
    active = {"id": "job-running", "status": "embedding", "object_key": "temp/u1/part1.docx"}
    store = _make_store(active_job=active)
    dispatched = []
    resp = _run_trigger(store, script, dispatched)

    assert resp.reused is True
    assert resp.job_id == "job-running"
    assert dispatched == []                          # 没有重复派发
    store.create_job.assert_not_called()


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
