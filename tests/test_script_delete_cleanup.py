"""DELETE /api/v1/scripts/{id} 删除剧本并清理导入副作用的回归测试。

核心诉求：删除接口不只做软删除，还要：
1. 物理清除 DM 解析产物（script_dm_* 全部挂 script_id 的行）；
2. 软删上传的手册文件记录（files 表，带用户过滤）；
3. 按引用计数（files 表 + 其它剧本的 dmGuide）决定是否物理删除 OSS 对象；
4. 软删剧本行并摘掉 extra.dmGuide。

用 Fake 仓储 / FakeDMStore 模拟存储，无需真实数据库。

可直接运行：python tests/test_script_delete_cleanup.py
也兼容 pytest：pytest tests/test_script_delete_cleanup.py
"""

import asyncio
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.exceptions import NotFoundError
from app.services.script_service import ScriptService

GUIDE_KEY = "uploads/u1/2024/01/dm-guide.pdf"


class _FakeOptions:
    """让 _label_map / _valid_codes 不依赖真实字典服务。"""

    async def get_tree(self):
        class _Tree:
            categories = []

        return _Tree()


class FakeRepository:
    def __init__(self, *, row=None, script_refs=0):
        self.row = row
        self.script_refs = script_refs
        self.soft_delete_calls = []
        self.count_refs_calls = []

    async def get(self, script_id, *, include_deleted=False):
        return dict(self.row) if self.row else None

    async def soft_delete(self, script_id, *, extra=None):
        self.soft_delete_calls.append((script_id, extra))
        return dict(self.row or {}, deleted_at="2024-01-02", status="offline")

    async def count_dm_guide_refs(self, object_key, *, exclude_script_id=None):
        self.count_refs_calls.append((object_key, exclude_script_id))
        return self.script_refs


class FakeFiles:
    def __init__(self, ref_count=0):
        self.ref_count = ref_count
        self.soft_deleted = []

    async def soft_delete(self, file_id, user_id):
        self.soft_deleted.append((file_id, user_id))

    async def count_references(self, object_key):
        return self.ref_count


class FakeOSS:
    def __init__(self):
        self.deleted = []

    async def delete_object(self, object_key):
        self.deleted.append(object_key)


class FakeDMStore:
    def __init__(self):
        self.purged = []

    def purge_script_side_effects(self, script_id):
        self.purged.append(script_id)


def _row_with_guide():
    return {
        "id": "script-1",
        "title": "雾都疑影",
        "code": "wu-du-yi-ying",
        "extra": {
            "dmGuide": {
                "objectKey": GUIDE_KEY,
                "fileId": "file-9",
                "fileName": "雾都疑影.pdf",
            }
        },
        "status": "published",
        "created_at": "2024-01-01",
        "updated_at": "2024-01-01",
    }


def _make_service(repo, files=None, oss=None):
    return ScriptService(
        repo=repo,
        option_service=_FakeOptions(),
        files=files or FakeFiles(),
        oss=oss or FakeOSS(),
    )


def _run(svc, script_id, user_id=""):
    with mock.patch(
        "app.services.script_service.store_mod.get_dm_store",
        return_value=FakeDMStore(),
    ), mock.patch(
        "app.services.script_service.cache.bump_scope_version_sync",
    ) as _bump:
        asyncio.run(svc.delete_script(script_id, user_id=user_id))
        return _bump


def test_delete_cleans_side_effects_and_object():
    repo = FakeRepository(row=_row_with_guide())
    files = FakeFiles(ref_count=0)
    oss = FakeOSS()
    svc = _make_service(repo, files=files, oss=oss)
    bump = _run(svc, "script-1", user_id="user-1")

    # 1) DM 解析产物被物理清
    assert files.soft_deleted == [("file-9", "user-1")]
    # 2) 文件记录软删（带用户过滤）
    assert files.soft_deleted == [("file-9", "user-1")]
    # 3) files 表与其它剧本引用均为 0 → OSS 对象物理删除
    assert oss.deleted == [GUIDE_KEY]
    # 4) 剧本软删除时 extra 摘掉了 dmGuide
    assert repo.soft_delete_calls[0][0] == "script-1"
    assert repo.soft_delete_calls[0][1] == {}
    # 5) QA 标题链缓存按剧本的 DM 聚合 code 失效
    assert bump.call_args_list and "wu-dou-yi-ying" in bump.call_args_list[0][0][0]


def test_delete_keeps_object_when_shared():
    repo = FakeRepository(row=_row_with_guide(), script_refs=1)
    files = FakeFiles(ref_count=1)
    oss = FakeOSS()
    svc = _make_service(repo, files=files, oss=oss)
    _run(svc, "script-1", user_id="user-1")

    # 还有文件记录 / 剧本在引用 → OSS 对象不删
    assert oss.deleted == []
    # 剧本行照常软删除
    assert repo.soft_delete_calls and repo.soft_delete_calls[0][0] == "script-1"


def test_delete_not_found():
    svc = _make_service(FakeRepository(row=None))
    try:
        asyncio.run(svc.delete_script("nope"))
        assert False, "应当抛 NotFoundError"
    except NotFoundError:
        pass


def test_delete_without_guide_skips_file_cleanup():
    row = _row_with_guide()
    row["extra"] = {}
    repo = FakeRepository(row=row)
    files = FakeFiles()
    oss = FakeOSS()
    svc = _make_service(repo, files=files, oss=oss)
    _run(svc, "script-1", user_id="user-1")

    assert files.soft_deleted == []
    assert oss.deleted == []
    # 没有 dmGuide 的剧本：软删除不传 extra（保持原样）
    assert repo.soft_delete_calls[0][1] is None


if __name__ == "__main__":
    import traceback

    for fn in (
        test_delete_cleans_side_effects_and_object,
        test_delete_keeps_object_when_shared,
        test_delete_not_found,
        test_delete_without_guide_skips_file_cleanup,
    ):
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            traceback.print_exc()
            print(f"FAIL {fn.__name__}")
