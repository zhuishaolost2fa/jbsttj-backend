"""POST /api/v1/scripts 撞 `uq_scripts_code` 时转为「补充更新」的回归测试。

核心诉求：重复提交同一个 code（或并发写入撞唯一约束）时，接口不应 500，
而是把本次提交合并进已有行，返回 was_created=False。

用 FakeRepository 模拟存储，无需真实数据库。

可直接运行：python tests/test_script_code_conflict.py
也兼容 pytest：pytest tests/test_script_code_conflict.py
"""

import asyncio
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.exceptions import DatabaseError
from app.schemas.script import ScriptCreate
from app.services.script_service import ScriptService, _is_unique_violation


class _FakeOptions:
    """让 _label_map / _valid_codes 不依赖真实字典服务。"""

    async def get_tree(self):
        class _Tree:
            categories = []

        return _Tree()


def _calls():
    return []


class FakeRepository:
    """可配置的假仓储，用于断言服务层在 code 冲突时的行为。"""

    def __init__(self, *, existing_by_code=None, create_raises=None):
        self.existing_by_code = existing_by_code or {}  # {code: row}
        self.create_raises = create_raises
        self.update_calls = []
        self.update_by_code_calls = []
        self.create_calls = []

    async def get_by_code(self, code, *, include_deleted=False):
        # 主动查重只命中非删除记录；include_deleted 时也能拿到被软删的
        row = self.existing_by_code.get(code)
        if row is None:
            return None
        if row.get("deleted_at") and not include_deleted:
            return None
        return dict(row)

    async def create(self, data):
        self.create_calls.append(dict(data))
        if self.create_raises is not None:
            raise self.create_raises
        return dict(data, id="new-id", created_at="2024-01-01", updated_at="2024-01-01")

    async def update(self, script_id, data):
        self.update_calls.append((script_id, dict(data)))
        # 默认成功返回合并后的行（保留已有行上的 code 等字段）
        base = next((r for r in self.existing_by_code.values()
                     if r.get("id") == script_id), {})
        return {**base, **data, "id": script_id, "updated_at": "2024-01-02"}

    async def update_by_code(self, code, data, *, include_deleted=False):
        self.update_by_code_calls.append((code, dict(data), include_deleted))
        base = self.existing_by_code.get(code, {})
        if base.get("deleted_at") and not include_deleted:
            return None
        return {**base, **data, "updated_at": "2024-01-02"}


def _make_service(repo):
    return ScriptService(repo=repo, option_service=_FakeOptions())


# 与 slugify("雾都疑影") 实际产出的 code 保持一致，避免拼音歧义导致查重 miss
CODE = "wu-dou-yi-ying"


def _payload(title="雾都疑影", code=None):
    return ScriptCreate(title=title, **({"code": code} if code else {}))


def test_is_unique_violation_detects_constraint():
    exc = DatabaseError(
        "dup",
        details={"code": "23505",
                 "message": 'duplicate key value violates unique constraint "uq_scripts_code"'},
    )
    assert _is_unique_violation(exc, "uq_scripts_code") is True
    assert _is_unique_violation(exc, "uq_other") is False

    # 非 23505 的唯一错误（别的约束）不应被误判
    other = DatabaseError("dup", details={"code": "23505",
                                          "message": 'violates unique constraint "uq_other"'})
    assert _is_unique_violation(other, "uq_scripts_code") is False


def test_proactive_code_dup_supplements_existing():
    """code 已存在于库里（主动查重命中）-> 直接补充更新，was_created=False。"""
    existing = {"id": "s1", "code": CODE, "title": "雾都疑影",
                "author": "张三", "aliases": ["雾都疑影"], "extra": {"a": 1}}
    repo = FakeRepository(existing_by_code={CODE: existing})
    service = _make_service(repo)

    # 这次提交换作者、并补一个别名与 extra 键
    payload = ScriptCreate(title="雾都疑影", code=CODE, author="李四",
                           aliases=["雾都疑影v2"], extra={"b": 2})
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    item, was_created = asyncio.run(
        service._create_or_supplement(payload, data, user_id="u1")
    )

    assert was_created is False
    # 走的是 update（按 id），不是 create
    assert repo.create_calls == []
    assert repo.update_calls and repo.update_calls[0][0] == "s1"
    # 标量提交覆盖、身份字段不变（code 等身份字段不进入合并数据，由 DB 行保留）
    merged = repo.update_calls[0][1]
    assert merged["author"] == "李四"
    assert "code" not in merged                          # code 不变，不参与合并
    assert "created_by" not in merged                    # 身份字段不参与合并
    # 数组并集、extra 深合并
    assert set(merged["aliases"]) == {"雾都疑影", "雾都疑影v2"}
    assert merged["extra"] == {"a": 1, "b": 2}


def test_concurrent_code_dup_falls_back_to_supplement():
    """主动查重没命中（被软删）、insert 撞唯一约束 -> 兜底按 code 复活补充更新。"""
    # 软删除状态下的重复行：主动查重（排除已删）命中不了，但 insert 仍撞唯一约束
    existing = {"id": "s2", "code": CODE, "title": "雾都疑影",
                "author": "张三", "deleted_at": "2024-01-01"}
    dup_error = DatabaseError(
        "dup",
        details={"code": "23505",
                 "message": 'duplicate key value violates unique constraint "uq_scripts_code"'},
    )
    repo = FakeRepository(existing_by_code={CODE: existing}, create_raises=dup_error)
    service = _make_service(repo)

    payload = _payload(code=CODE)
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    item, was_created = asyncio.run(
        service._create_or_supplement(payload, data, user_id="u1")
    )

    assert was_created is False
    # 兜底路径：按 code 更新且 include_deleted=True（复活被软删的重复行）
    assert repo.update_by_code_calls == [(CODE, mock.ANY, True)]
    assert item.code == CODE


def test_fresh_code_creates_new():
    """code 不存在 -> 正常新建，was_created=True。"""
    repo = FakeRepository()  # 没有任何 existing
    service = _make_service(repo)
    payload = _payload()
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    item, was_created = asyncio.run(
        service._create_or_supplement(payload, data, user_id="u1")
    )
    assert was_created is True
    assert repo.create_calls  # 确实走了 insert


def test_non_code_database_error_still_raises():
    """非 code 相关的数据库错误不应被吞掉。"""
    repo = FakeRepository(create_raises=DatabaseError("boom", details={"code": "42P01"}))
    service = _make_service(repo)
    payload = _payload()
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    try:
        asyncio.run(service._create_or_supplement(payload, data, user_id="u1"))
        assert False, "应抛出异常"
    except DatabaseError as e:
        assert "boom" in str(e)


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
