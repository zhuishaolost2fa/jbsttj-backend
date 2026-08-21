"""剧本「求解析」服务层核心逻辑测试。

覆盖：去重（同用户同剧本只能一条）、幂等复用、取消后复活、
已完成拦截、取消语义、已完成状态惰性同步（库内批量 + 库外标题回填）、
并发唯一约束兜底。

用 Fake 仓储模拟存储，无需真实数据库。

可直接运行：python tests/test_script_request_service.py
也兼容 pytest：pytest tests/test_script_request_service.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.exceptions import ConflictError, DatabaseError, NotFoundError
from app.schemas.script_request import ScriptRequestCreate
from app.services.script_request_service import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    ScriptRequestService,
)

_UQ = "uq_script_requests_user_script"


def _dup_error():
    return DatabaseError(
        "dup",
        details={
            "code": "23505",
            "message": f'duplicate key value violates unique constraint "{_UQ}"',
        },
    )


class FakeScriptsRepo:
    """剧本仓储假实现：get / get_by_code / find_existing。"""

    def __init__(self, scripts=None):
        self.scripts = scripts or {}  # {id: row}

    async def get(self, script_id, *, include_deleted=False):
        row = self.scripts.get(script_id)
        return dict(row) if row else None

    async def get_by_code(self, code, *, include_deleted=False):
        for row in self.scripts.values():
            if row.get("code") == code:
                return dict(row)
        return None

    async def find_existing(self, title):
        for row in self.scripts.values():
            if row.get("title") == title or title in (row.get("aliases") or []):
                return dict(row)
        return None

    async def get_scripts(self, script_ids):
        """批量取剧本行：与 ScriptRepository.get_scripts 语义一致。"""
        ids = {str(i) for i in script_ids}
        return [dict(r) for r in self.scripts.values() if str(r.get("id")) in ids]


class FakeRequestRepo:
    """求解析仓储假实现，记录调用并支持可配置行为。"""

    def __init__(self, *, rows=None, indexed_ids=None, create_raises=None):
        # rows: {id: row}，行内必须含 id/user_id/match_key/script_title/status...
        self.rows = dict(rows or {})
        self.indexed_ids = set(indexed_ids or ())
        self.create_raises = create_raises
        self.next_id = 1
        self.create_calls = []
        self.update_calls = []
        self.mark_completed_calls = []

    def _find_by_match_key(self, user_id, match_key):
        for row in self.rows.values():
            if row.get("user_id") == user_id and row.get("match_key") == match_key:
                return dict(row)
        return None

    async def get(self, request_id, user_id=None):
        row = self.rows.get(request_id)
        if row is None:
            return None
        if user_id and row.get("user_id") != user_id:
            return None
        return dict(row)

    async def find_by_match_key(self, user_id, match_key):
        return self._find_by_match_key(user_id, match_key)

    async def create(self, payload):
        self.create_calls.append(dict(payload))
        if self.create_raises is not None:
            raise self.create_raises
        rid = f"r{self.next_id}"
        self.next_id += 1
        row = {
            "id": rid,
            "user_id": payload.get("user_id"),
            "match_key": payload.get("match_key"),
            "script_id": payload.get("script_id"),
            "script_code": payload.get("script_code"),
            "script_title": payload.get("script_title"),
            "reason": payload.get("reason"),
            "status": payload.get("status", STATUS_PENDING),
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        self.rows[rid] = row
        return dict(row)

    async def update(self, request_id, data, user_id=None):
        self.update_calls.append((request_id, dict(data), user_id))
        row = self.rows.get(request_id)
        if row is None:
            return None
        if user_id and row.get("user_id") != user_id:
            return None
        row = {**row, **data}
        self.rows[request_id] = row
        return dict(row)

    async def list_indexed_script_ids(self):
        return set(self.indexed_ids)

    async def list_pending(self):
        return [dict(r) for r in self.rows.values() if r.get("status") == STATUS_PENDING]

    async def mark_completed_by_script_ids(self, script_ids, completed_at):
        self.mark_completed_calls.append((list(script_ids), completed_at))
        n = 0
        for row in self.rows.values():
            if (
                row.get("status") == STATUS_PENDING
                and row.get("script_id") in script_ids
            ):
                row["status"] = STATUS_COMPLETED
                row["completed_at"] = completed_at
                n += 1
        return n

    async def leaderboard_rows(self):
        from collections import Counter

        counts = Counter()
        titles = {}
        for row in self.rows.values():
            if row.get("status") != STATUS_PENDING:
                continue
            key = row["match_key"]
            counts[key] += 1
            titles[key] = row["script_title"]
        out = []
        for key, cnt in counts.items():
            row = next(
                (r for r in self.rows.values() if r.get("match_key") == key), {}
            )
            out.append(
                {
                    "match_key": key,
                    "script_title": titles[key],
                    "script_code": row.get("script_code"),
                    "script_id": row.get("script_id"),
                    "count": cnt,
                }
            )
        return out

    async def list_by_user(self, user_id, *, status=None, limit=20, offset=0):
        rows = [dict(r) for r in self.rows.values() if r.get("user_id") == user_id]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows[offset : offset + limit], len(rows)


def _service(req_repo, scripts_repo=None):
    return ScriptRequestService(
        repo=req_repo, scripts=scripts_repo or FakeScriptsRepo()
    )


def _payload(title="雾都疑影", **kw):
    return ScriptRequestCreate(script_title=title, **kw)


# ---------------------------------------------------------------
# 发起求解析
# ---------------------------------------------------------------
def test_create_library_outside_uses_title_key():
    """库外剧本：match_key 用归一化标题键，新建成功。"""
    repo = FakeRequestRepo()
    service = _service(repo)
    item = asyncio.run(service.create("u1", _payload(title="雾都疑影")))
    assert item.script_title == "雾都疑影"
    assert item.script_id is None
    assert item.status == STATUS_PENDING
    assert item.already_exists is False
    # 归一化标题键（小写）作为去重键
    assert repo.create_calls[0]["match_key"] == "雾都疑影".lower()


def test_create_linked_script_uses_script_id_key():
    """传 scriptId：match_key 用 script_id，并回填真实标题 / code。"""
    scripts = FakeScriptsRepo(
        {"s1": {"id": "s1", "code": "wu-dou", "title": "雾都疑影"}}
    )
    repo = FakeRequestRepo()
    service = _service(repo, scripts)
    item = asyncio.run(
        service.create("u1", _payload(title="随便写的标题", script_id="s1"))
    )
    assert item.script_id == "s1"
    assert item.script_code == "wu-dou"
    assert item.script_title == "雾都疑影"  # 回填库里真实标题
    assert repo.create_calls[0]["match_key"] == "s1"


def test_create_script_code_resolves():
    scripts = FakeScriptsRepo(
        {"s1": {"id": "s1", "code": "wu-dou", "title": "雾都疑影"}}
    )
    repo = FakeRequestRepo()
    service = _service(repo, scripts)
    item = asyncio.run(service.create("u1", _payload(script_code="wu-dou")))
    assert item.script_id == "s1"
    assert item.script_code == "wu-dou"


def test_create_title_matches_existing_script():
    """只传标题但库里已有同名剧本：关联到该剧本，避免产生两条诉求。"""
    scripts = FakeScriptsRepo(
        {"s1": {"id": "s1", "code": "wu-dou", "title": "雾都疑影"}}
    )
    repo = FakeRequestRepo()
    service = _service(repo, scripts)
    item = asyncio.run(service.create("u1", _payload(title="雾都疑影")))
    assert item.script_id == "s1"
    assert repo.create_calls[0]["match_key"] == "s1"


def test_create_rejects_already_parsed_script():
    """剧本已解析：直接拦截，返回 409 script_already_parsed。"""
    scripts = FakeScriptsRepo(
        {"s1": {"id": "s1", "code": "wu-dou", "title": "雾都疑影"}}
    )
    repo = FakeRequestRepo(indexed_ids={"s1"})
    service = _service(repo, scripts)
    try:
        asyncio.run(service.create("u1", _payload(script_id="s1")))
        assert False, "应抛出 ConflictError"
    except ConflictError as e:
        assert e.code == "script_already_parsed"
    assert repo.create_calls == []


def test_create_duplicate_pending_is_idempotent():
    """重复求（仍 pending）：返回既有记录，already_exists=True，不新建。"""
    repo = FakeRequestRepo(
        rows={
            "r1": {
                "id": "r1",
                "user_id": "u1",
                "match_key": "雾都疑影",
                "script_id": None,
                "script_code": None,
                "script_title": "雾都疑影",
                "status": STATUS_PENDING,
            }
        }
    )
    service = _service(repo)
    item = asyncio.run(service.create("u1", _payload(title="雾都疑影")))
    assert item.already_exists is True
    assert item.id == "r1"
    assert repo.create_calls == []  # 未新建


def test_create_revives_cancelled():
    """已取消的诉求再次发起：复活回 pending，不新建行。"""
    repo = FakeRequestRepo(
        rows={
            "r1": {
                "id": "r1",
                "user_id": "u1",
                "match_key": "雾都疑影",
                "script_id": None,
                "script_code": None,
                "script_title": "雾都疑影",
                "status": STATUS_CANCELLED,
                "cancelled_at": "2026-01-01",
            }
        }
    )
    service = _service(repo)
    item = asyncio.run(service.create("u1", _payload(title="雾都疑影")))
    assert item.already_exists is True
    assert item.id == "r1"
    assert repo.rows["r1"]["status"] == STATUS_PENDING
    assert repo.rows["r1"]["cancelled_at"] is None
    assert repo.create_calls == []


def test_create_completed_script_conflicts():
    """诉求已完成（剧本已解析）再求：409。"""
    repo = FakeRequestRepo(
        rows={
            "r1": {
                "id": "r1",
                "user_id": "u1",
                "match_key": "雾都疑影",
                "script_id": None,
                "script_code": None,
                "script_title": "雾都疑影",
                "status": STATUS_COMPLETED,
            }
        }
    )
    service = _service(repo)
    try:
        asyncio.run(service.create("u1", _payload(title="雾都疑影")))
        assert False, "应抛出 ConflictError"
    except ConflictError as e:
        assert e.code == "script_already_parsed"


def test_create_concurrent_dup_falls_back():
    """并发撞唯一约束：读回既有行走统一分支（不 500）。"""
    repo = FakeRequestRepo(
        rows={
            "r1": {
                "id": "r1",
                "user_id": "u1",
                "match_key": "雾都疑影",
                "script_id": None,
                "script_code": None,
                "script_title": "雾都疑影",
                "status": STATUS_PENDING,
            }
        },
        create_raises=_dup_error(),
    )
    service = _service(repo)
    item = asyncio.run(service.create("u1", _payload(title="雾都疑影")))
    assert item.id == "r1"
    assert item.already_exists is True


# ---------------------------------------------------------------
# 取消求解析
# ---------------------------------------------------------------
def test_cancel_pending():
    repo = FakeRequestRepo(
        rows={
            "r1": {
                "id": "r1",
                "user_id": "u1",
                "match_key": "k",
                "script_title": "雾都疑影",
                "status": STATUS_PENDING,
            }
        }
    )
    service = _service(repo)
    item = asyncio.run(service.cancel("u1", "r1"))
    assert item.status == STATUS_CANCELLED
    assert item.cancelled_at is not None


def test_cancel_not_owner_404():
    repo = FakeRequestRepo(
        rows={
            "r1": {
                "id": "r1",
                "user_id": "u2",
                "match_key": "k",
                "script_title": "雾都疑影",
                "status": STATUS_PENDING,
            }
        }
    )
    service = _service(repo)
    try:
        asyncio.run(service.cancel("u1", "r1"))
        assert False, "应抛出 NotFoundError"
    except NotFoundError:
        pass


def test_cancel_completed_rejected():
    repo = FakeRequestRepo(
        rows={
            "r1": {
                "id": "r1",
                "user_id": "u1",
                "match_key": "k",
                "script_title": "雾都疑影",
                "status": STATUS_COMPLETED,
            }
        }
    )
    service = _service(repo)
    try:
        asyncio.run(service.cancel("u1", "r1"))
        assert False, "应抛出 ConflictError"
    except ConflictError as e:
        assert e.code == "request_completed"


def test_cancel_already_cancelled_idempotent():
    repo = FakeRequestRepo(
        rows={
            "r1": {
                "id": "r1",
                "user_id": "u1",
                "match_key": "k",
                "script_title": "雾都疑影",
                "status": STATUS_CANCELLED,
            }
        }
    )
    service = _service(repo)
    item = asyncio.run(service.cancel("u1", "r1"))
    assert item.status == STATUS_CANCELLED
    assert repo.update_calls == []  # 不重复更新


# ---------------------------------------------------------------
# 已完成状态惰性同步
# ---------------------------------------------------------------
def test_sync_completed_direct_script_ids():
    """剧本已解析：script_id 关联的 pending 诉求批量置 completed。"""
    repo = FakeRequestRepo(
        rows={
            "r1": {
                "id": "r1",
                "user_id": "u1",
                "match_key": "s1",
                "script_id": "s1",
                "script_title": "雾都疑影",
                "status": STATUS_PENDING,
            },
            "r2": {
                "id": "r2",
                "user_id": "u2",
                "match_key": "s2",
                "script_id": "s2",
                "script_title": "另一个本",
                "status": STATUS_PENDING,
            },
        },
        indexed_ids={"s1"},
    )
    service = _service(repo)
    asyncio.run(service._sync_completed())
    assert repo.rows["r1"]["status"] == STATUS_COMPLETED
    assert repo.rows["r1"]["completed_at"] is not None
    assert repo.rows["r2"]["status"] == STATUS_PENDING  # 未解析的保持不动
    assert repo.mark_completed_calls and repo.mark_completed_calls[0][0] == ["s1"]


def test_sync_completed_backfill_unlinked():
    """库外诉求：已解析剧本标题匹配后回填 script_id 并置 completed。"""
    scripts = FakeScriptsRepo(
        {"s1": {"id": "s1", "code": "wu-dou", "title": "雾都疑影", "aliases": []}}
    )
    repo = FakeRequestRepo(
        rows={
            "r1": {
                "id": "r1",
                "user_id": "u1",
                "match_key": "雾都疑影",
                "script_id": None,
                "script_title": "雾都疑影",
                "status": STATUS_PENDING,
            }
        },
        indexed_ids={"s1"},
    )
    service = _service(repo, scripts)
    asyncio.run(service._sync_completed())
    row = repo.rows["r1"]
    assert row["status"] == STATUS_COMPLETED
    assert row["script_id"] == "s1"
    assert row["script_code"] == "wu-dou"


# ---------------------------------------------------------------
# 排行榜
# ---------------------------------------------------------------
def test_leaderboard_aggregates_pending_only():
    """排行榜按剧本聚合 pending 诉求，取消 / 已完成不计入。"""
    repo = FakeRequestRepo(
        rows={
            "r1": {
                "id": "r1",
                "user_id": "u1",
                "match_key": "a",
                "script_id": None,
                "script_title": "剧本A",
                "status": STATUS_PENDING,
            },
            "r2": {
                "id": "r2",
                "user_id": "u2",
                "match_key": "a",
                "script_id": None,
                "script_title": "剧本A",
                "status": STATUS_PENDING,
            },
            "r3": {
                "id": "r3",
                "user_id": "u3",
                "match_key": "b",
                "script_id": None,
                "script_title": "剧本B",
                "status": STATUS_CANCELLED,  # 不计入
            },
        }
    )
    service = _service(repo)
    result = asyncio.run(service.leaderboard())
    assert result.pagination.total == 1
    assert result.items[0].script_title == "剧本A"
    assert result.items[0].request_count == 2


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
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
