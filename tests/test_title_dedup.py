"""剧本标题归一化去重的回归测试。

验证两件事：
1. normalize_title_key 把系统噪音（扩展名 / 副本 /（1））归一成同一把钥匙，
   但保留用户有意写的续集序号（山母鬼2 / 山母鬼 第二部）；
2. ScriptRepository.find_existing 用归一化钥匙匹配，让「山母鬼.pdf」这类重传
   命中已存在的「山母鬼」行，从而在导入时合并回原行，而不是生成 shan-mu-gui-2。

可直接运行：python tests/test_title_dedup.py
也兼容 pytest：pytest tests/test_title_dedup.py
"""

import asyncio
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.repository import ScriptRepository, normalize_title_key


def test_normalize_title_key_collapses_system_noise():
    noise = ["山母鬼.pdf", "山母鬼 副本", "山母鬼（1）", "山母鬼 副本（1）",
             "  山母鬼  ", "山母鬼.DOCX"]
    for n in noise:
        assert normalize_title_key(n) == "山母鬼", n


def test_normalize_title_key_preserves_real_sequels():
    # 用户有意写的续集序号必须保留，否则会被误判成重传
    assert normalize_title_key("山母鬼2") == "山母鬼2"
    assert normalize_title_key("山母鬼 第二部") == "山母鬼 第二部"
    assert normalize_title_key("山母鬼：终章") == "山母鬼：终章"


def test_find_existing_matches_normalized_title():
    captured = {}

    class _FakeDb:
        async def select(self, table, *, filters=None, order=None, limit=None, offset=None):
            captured["filters"] = filters
            # 模拟库里已有一行干净的「山母鬼」
            return [{"id": "s1", "title": "山母鬼", "aliases": ["山母鬼"]}]

    repo = ScriptRepository(db=_FakeDb())
    row = asyncio.run(repo.find_existing("山母鬼.pdf"))
    assert row is not None and row["id"] == "s1"
    # 必须用归一化钥匙同时去匹配标题与别名
    assert "aliases.cs.{山母鬼}" in captured["filters"]["or"]


def test_find_existing_returns_none_for_unknown_title():
    class _FakeDb:
        async def select(self, table, *, filters=None, order=None, limit=None, offset=None):
            return []

    repo = ScriptRepository(db=_FakeDb())
    assert asyncio.run(repo.find_existing("完全不相关的本子")) is None


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
