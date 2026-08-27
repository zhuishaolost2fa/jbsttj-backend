"""DELETE /api/v1/files/{id} 删除 DM 手册文件时联动清理解析产物的回归测试。

核心诉求：删的不只是文件记录——若该文件是某剧本的 DM 手册（extra.dmGuide
引用它），还要：
1. 物理清除该剧本的 DM 解析产物（jobs/documents/chunks/qa/stories/highlights）；
2. 摘掉剧本行的 extra.dmGuide（剧本行本身保留，可重新上传导入）；
3. 失效 QA 标题链缓存；
4. 上述清理全程 best-effort，失败不影响文件删除本身。

秒传语义：OSS 对象仍被其它文件记录引用时不物理删对象，且 objectKey 级
兜底匹配不生效（只按 fileId 命中），避免误伤共享同一对象的其它剧本。

用 Fake 仓储 / FakeDMStore 模拟存储，无需真实数据库。

可直接运行：python tests/test_file_delete_dm_cleanup.py
也兼容 pytest：pytest tests/test_file_delete_dm_cleanup.py
"""

import asyncio
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.exceptions import NotFoundError
from app.services.file_service import FileService

GUIDE_KEY = "uploads/u1/2024/01/dm-guide.pdf"


class FakeFilesRepo:
    def __init__(self, *, row=None, ref_count=0):
        self.row = row
        self.ref_count = ref_count
        self.soft_deleted = []

    async def get(self, file_id, *, user_id=None):
        if self.row and self.row["id"] == file_id:
            return dict(self.row)
        return None

    async def soft_delete(self, file_id, user_id):
        self.soft_deleted.append((file_id, user_id))

    async def count_references(self, object_key):
        return self.ref_count


class FakeOSS:
    def __init__(self):
        self.deleted = []

    async def delete_object(self, object_key):
        self.deleted.append(object_key)


class FakeScriptsRepo:
    """承接 purge_dm_guide_for_file 的仓储调用（find + update）。"""

    def __init__(self, scripts=None):
        # scripts: find_dm_guide_scripts 的返回行（含 id/title/extra）
        self.scripts = scripts or []
        self.find_calls = []
        self.updates = []

    async def find_dm_guide_scripts(self, file_id, *, object_key=None, include_object_refs=False):
        self.find_calls.append((file_id, object_key, include_object_refs))
        return [dict(s) for s in self.scripts]

    async def update(self, script_id, data):
        self.updates.append((script_id, data))
        return {"id": script_id, **data}


class FakeDMStore:
    def __init__(self):
        self.purged = []

    def purge_script_side_effects(self, script_id):
        self.purged.append(script_id)


def _file_row(file_id="file-9", object_key=GUIDE_KEY):
    return {
        "id": file_id,
        "object_key": object_key,
        "filename": "雾都疑影.pdf",
        "user_id": "user-1",
    }


def _script_row(script_id="script-1", title="雾都疑影", file_id="file-9"):
    return {
        "id": script_id,
        "title": title,
        "extra": {
            "dmGuide": {"objectKey": GUIDE_KEY, "fileId": file_id, "fileName": "雾都疑影.pdf"}
        },
    }


def _make_service(files, oss=None):
    return FileService(oss=oss or FakeOSS(), settings=mock.Mock(), files=files)


def _run(svc, file_id, user_id="user-1", scripts_repo=None, purge=True):
    store = FakeDMStore()
    with mock.patch(
        "app.services.script_service.store_mod.get_dm_store", return_value=store
    ), mock.patch(
        "app.services.script_service.ScriptRepository",
        return_value=scripts_repo or FakeScriptsRepo(),
    ), mock.patch(
        "app.services.script_service.cache.bump_scope_version_sync"
    ) as bump:
        asyncio.run(svc.delete_file(_user(user_id), file_id, purge=purge))
        return store, bump


class _user:
    def __init__(self, uid):
        self.id = uid


def test_delete_file_purges_dm_guide_side_effects():
    """文件被剧本当手册引用 → 解析产物被清、dmGuide 被摘、缓存被失效。"""
    files = FakeFilesRepo(row=_file_row(), ref_count=0)
    scripts = FakeScriptsRepo(scripts=[_script_row()])
    oss = FakeOSS()
    svc = _make_service(files, oss=oss)
    store, bump = _run(svc, "file-9", scripts_repo=scripts)

    # 1) DM 解析产物物理清（7 张表由 purge_script_side_effects 统一处理）
    assert store.purged == ["script-1"]
    # 2) 剧本行保留，但 extra 摘掉了 dmGuide
    assert scripts.updates and scripts.updates[0][0] == "script-1"
    assert scripts.updates[0][1]["extra"] == {}
    # 3) QA 标题链缓存按 DM 聚合 code 失效（雾都疑影 -> wu-dou-yi-ying）
    assert bump.call_args_list and "wu-dou-yi-ying" in bump.call_args_list[0][0][0]
    # 4) 无其它引用 → OSS 对象物理删除
    assert oss.deleted == [GUIDE_KEY]
    # 5) 文件记录本身软删
    assert files.soft_deleted == [("file-9", "user-1")]


def test_delete_file_shared_object_keeps_oss_and_object_refs():
    """秒传共享：对象仍被引用 → 不删 OSS；兜底 objectKey 匹配不启用。"""
    files = FakeFilesRepo(row=_file_row(), ref_count=1)
    scripts = FakeScriptsRepo(scripts=[_script_row()])
    oss = FakeOSS()
    svc = _make_service(files, oss=oss)
    store, _ = _run(svc, "file-9", scripts_repo=scripts)

    # OSS 对象保留
    assert oss.deleted == []
    # fileId 直接命中 → 解析产物仍要清
    assert store.purged == ["script-1"]
    # include_object_refs=False（对象没死，objectKey 级兜底不生效）
    assert scripts.find_calls[0][2] is False


def test_delete_file_without_guide_refs_noop():
    """普通文件（无剧本引用）→ 只走文件删除，不触发 DM 清理。"""
    files = FakeFilesRepo(row=_file_row(), ref_count=0)
    scripts = FakeScriptsRepo(scripts=[])
    oss = FakeOSS()
    svc = _make_service(files, oss=oss)
    store, bump = _run(svc, "file-9", scripts_repo=scripts)

    assert store.purged == []
    assert scripts.updates == []
    assert bump.call_args_list == []
    # 无引用 → 对象照常物理删除
    assert oss.deleted == [GUIDE_KEY]


def test_delete_file_purge_false_still_cleans_dm_refs():
    """purge=false：对象保留，但文件记录已软删，fileId 命中的剧本仍要清。"""
    files = FakeFilesRepo(row=_file_row(), ref_count=0)
    scripts = FakeScriptsRepo(scripts=[_script_row()])
    oss = FakeOSS()
    svc = _make_service(files, oss=oss)
    store, _ = _run(svc, "file-9", scripts_repo=scripts, purge=False)

    assert oss.deleted == []  # 不动 OSS
    assert files.soft_deleted == [("file-9", "user-1")]  # 记录照删
    assert store.purged == ["script-1"]  # 解析产物仍清
    # object_dead=False → objectKey 兜底不启用
    assert scripts.find_calls[0][2] is False


def test_delete_file_not_found():
    svc = _make_service(FakeFilesRepo(row=None))
    try:
        asyncio.run(svc.delete_file(_user("user-1"), "nope"))
        assert False, "应当抛 NotFoundError"
    except NotFoundError:
        pass


if __name__ == "__main__":
    import traceback

    for fn in (
        test_delete_file_purges_dm_guide_side_effects,
        test_delete_file_shared_object_keeps_oss_and_object_refs,
        test_delete_file_without_guide_refs_noop,
        test_delete_file_purge_false_still_cleans_dm_refs,
        test_delete_file_not_found,
    ):
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            traceback.print_exc()
            print(f"FAIL {fn.__name__}")
