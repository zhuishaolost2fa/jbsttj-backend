"""SupabaseAuth 按邮箱反查用户的分页行为。

这里锁住一个**真实踩到过**的 bug：GoTrue 的 listUsers 返回体里**没有 total 字段**
（实测为 None）。分页终止条件若写成依赖 total：

    total = int(payload.get("total") or 0)        # → 0
    if len(users) < per_page or page * per_page >= total:   # 满页时恒真 → break

用户数超过一页后，第一页满页就 break，后面的账号全部漏判。
后果是绑定邮箱时「邮箱已被其他账号使用」的校验失效，会把别人的登录邮箱顶掉。

正确做法只看「本页是否满」：``len(users) < per_page`` 即已到最后一页。

注：项目未引入 pytest-asyncio，这里用 asyncio.run 同步包裹，不新增依赖。
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import get_settings
from app.services.supabase import SupabaseAuth


def _patch_client(monkeypatch, pages: list[list[dict]]):
    """把 httpx.AsyncClient 换成按页返回预设数据的假实现，并记录每次请求参数。"""
    calls: list[dict] = []

    class FakeResp:
        def __init__(self, users: list[dict], status_code: int = 200) -> None:
            self._users = users
            self.status_code = status_code

        def json(self) -> dict:
            # 刻意不带 total —— 与真实 GoTrue 一致
            return {"users": self._users}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url, headers=None, params=None):
            calls.append(dict(params or {}))
            page = int((params or {}).get("page", 1))
            return FakeResp(pages[page - 1] if page <= len(pages) else [])

    monkeypatch.setattr("app.services.supabase.httpx.AsyncClient", FakeAsyncClient)
    return calls


@pytest.fixture
def auth() -> SupabaseAuth:
    return SupabaseAuth(get_settings())


def test_finds_user_across_pages(auth, monkeypatch):
    """目标在最后一页：分页必须继续推进，不能第一页满页就停。"""
    pages = [
        [{"id": f"u{i}", "email": f"p{i}@x.com"} for i in range(1, 4)],  # 满页
        [{"id": "u4", "email": "p4@x.com"}, {"id": "u5", "email": "target@x.com"}],
    ]
    calls = _patch_client(monkeypatch, pages)

    got = asyncio.run(auth.find_user_id_by_email("target@x.com", per_page=3))

    assert got == "u5"
    assert len(calls) == 2, "第一页满页时应继续翻第二页"


def test_stops_at_last_page(auth, monkeypatch):
    """最后一页不满时停止，不多打无效请求。"""
    pages = [[{"id": "u1", "email": "a@x.com"}, {"id": "u2", "email": "b@x.com"}]]
    calls = _patch_client(monkeypatch, pages)

    got = asyncio.run(auth.find_user_id_by_email("b@x.com", per_page=5))

    assert got == "u2"
    assert len(calls) == 1


def test_returns_empty_when_not_found(auth, monkeypatch):
    """找不到必须返回空串 —— 调用方用它判断「邮箱未被占用」。"""
    _patch_client(monkeypatch, [[{"id": "u1", "email": "a@x.com"}]])

    assert asyncio.run(auth.find_user_id_by_email("nope@x.com", per_page=5)) == ""


def test_case_insensitive_and_trimmed(auth, monkeypatch):
    _patch_client(monkeypatch, [[{"id": "u1", "email": "Someone@Example.com"}]])

    assert asyncio.run(auth.find_user_id_by_email("  SOMEONE@example.com  ")) == "u1"


def test_empty_email_short_circuits(auth, monkeypatch):
    def boom(*args, **kwargs):  # pragma: no cover - 不该被调用
        raise AssertionError("空邮箱不应发起网络请求")

    monkeypatch.setattr("app.services.supabase.httpx.AsyncClient", boom)
    assert asyncio.run(auth.find_user_id_by_email("")) == ""


def test_http_error_returns_empty_instead_of_raising(auth, monkeypatch):
    """接口异常时返回空串，让调用方走「未占用」分支并显式报错，而不是 500。"""

    class FakeResp:
        status_code = 500

        def json(self) -> dict:
            return {"message": "boom"}

    class FakeAsyncClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr("app.services.supabase.httpx.AsyncClient", FakeAsyncClient)
    assert asyncio.run(auth.find_user_id_by_email("a@x.com")) == ""
