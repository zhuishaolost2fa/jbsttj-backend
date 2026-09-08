"""详情页只读接口的 Redis 缓存覆盖与失效。

关注点不是「Redis 能不能连上」（redis_cache 本身有降级），而是**业务侧口径**：
  - 只读接口第二次请求是否命中缓存（不再打数据库）；
  - 写操作是否 bump 了正确的 scope（否则用户会读到过期数据）；
  - 解析中的状态是否**不**进缓存（否则前端看到卡住的进度）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest import mock

from app.core.config import Settings
from app.schemas.dm_guide import CreateHighlightRequest
from app.services import dm_service
from app.services import dm_store as store_mod
from app.services.dm_service import DMGuideService

CODE = "mao-dao-mou-sha-xun-huan"


class _MemCache:
    """内存版缓存替身：记录读写与 bump，行为与 redis_cache 一致（含 scope 版本）。"""

    def __init__(self) -> None:
        self.data: Dict[str, str] = {}
        self.versions: Dict[str, int] = {}
        self.bumped: List[str] = []

    async def get_scope_version(self, scope: str) -> int:
        return self.versions.get(scope, 0)

    async def cache_get_model(self, key: str, model_cls: Any) -> Optional[Any]:
        raw = self.data.get(key)
        if raw is None:
            return None
        return model_cls.model_validate_json(raw)

    async def cache_set_model(self, key: str, value: Any, ttl_seconds: int) -> None:
        self.data[key] = value.model_dump_json()

    async def bump_scope_versions(self, scopes: Any) -> None:
        for scope in scopes or ():
            if not scope:
                continue
            self.bumped.append(scope)
            self.versions[scope] = self.versions.get(scope, 0) + 1

    def keys_startswith(self, prefix: str) -> List[str]:
        return [k for k in self.data if k.startswith(prefix)]


class _FakeStore:
    def __init__(self, *, job_status: str = "completed") -> None:
        self.calls: List[str] = []
        self.job_status = job_status
        self.story_row = {
            "id": "story-1",
            "document_id": "doc-1",
            "script_code": CODE,
            "story_index": 0,
            "story_type": "timeline",
            "title": "开场",
            "content": "正文",
            "public_highlights": 0,
        }

    # ---- 故事还原 ----
    def list_stories(
        self, script_code: str, story_type: Optional[str] = None, limit: int = 50, offset: int = 0
    ) -> tuple[List[Dict[str, Any]], int]:
        self.calls.append("list_stories")
        return [self.story_row], 1

    def list_story_cards_by_ids(self, ids: List[str]) -> List[Dict[str, Any]]:
        self.calls.append("list_story_cards_by_ids")
        return [self.story_row]

    def get_story(self, story_id: str) -> Optional[Dict[str, Any]]:
        return self.story_row

    def count_highlights(self, story_id: str) -> int:
        return 0

    def list_highlights(self, **kwargs: Any) -> tuple[List[Dict[str, Any]], int]:
        self.calls.append("list_highlights")
        return [], 0

    def get_stories_by_ids(self, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        return {}

    def get_profiles(self, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        return {}

    def create_highlight(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append("create_highlight")
        return {
            "id": "hl-1",
            "story_id": payload["story_id"],
            "script_code": payload["script_code"],
            "user_id": payload["user_id"],
            "quote": payload["quote"],
            "start_offset": 0,
            "end_offset": 4,
            "visibility": payload["visibility"],
        }

    # ---- 状态 ----
    def list_active_documents_by_code(self, script_code: str) -> List[Dict[str, Any]]:
        self.calls.append("list_active_documents_by_code")
        return [{"id": "doc-1", "total_chunks": 10, "total_qa": 20, "total_stories": 3}]

    def latest_job(self, script_id: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        self.calls.append("latest_job")
        if not self.job_status:
            return None
        return {
            "id": "job-1",
            "script_id": script_id,
            "script_code": CODE,
            "status": self.job_status,
            "total_chunks": 10,
            "embedded_chunks": 10,
        }


class _FakeScript:
    id = "script-1"
    title = "猫岛谋杀循环"
    extra: Dict[str, Any] = {}


def _patch_cache(mem: _MemCache):
    """把 dm_service 里的 cache 换成内存版。"""
    return mock.patch.multiple(
        dm_service.cache,
        get_scope_version=mem.get_scope_version,
        cache_get_model=mem.cache_get_model,
        cache_set_model=mem.cache_set_model,
        bump_scope_versions=mem.bump_scope_versions,
    )


def _service(store: _FakeStore) -> DMGuideService:
    return DMGuideService(Settings(dm_rag_enabled=True))


def test_list_stories_hits_cache_second_time():
    store = _FakeStore()
    mem = _MemCache()
    svc = _service(store)

    async def run() -> None:
        with _patch_cache(mem), mock.patch.object(
            store_mod, "get_dm_store", return_value=store
        ):
            first = await svc.list_stories(script_code=CODE)
            second = await svc.list_stories(script_code=CODE)
        assert first.total == second.total == 1
        assert second.items[0].title == "开场"

    asyncio.run(run())
    # 第二次命中缓存，数据库只被查了一次
    assert store.calls.count("list_stories") == 1


def test_story_ids_lookup_hits_cache():
    store = _FakeStore()
    mem = _MemCache()
    svc = _service(store)

    async def run() -> None:
        with _patch_cache(mem), mock.patch.object(
            store_mod, "get_dm_store", return_value=store
        ):
            await svc.list_stories(script_code=CODE, ids=["story-1"])
            await svc.list_stories(script_code=CODE, ids=["story-1"])

    asyncio.run(run())
    assert store.calls.count("list_story_cards_by_ids") == 1


def test_create_highlight_bumps_story_and_list_scopes():
    store = _FakeStore()
    mem = _MemCache()
    svc = _service(store)

    async def run() -> None:
        with _patch_cache(mem), mock.patch.object(
            store_mod, "get_dm_store", return_value=store
        ):
            await svc.create_highlight(
                user_id="user-1",
                payload=CreateHighlightRequest(
                    storyId="story-1", quote="正文", startOffset=0, endOffset=2,
                    visibility="public",
                ),
            )

    asyncio.run(run())
    # 条目详情 + 共读时间线（按 code 与按 story_id 两种查法）+ 故事列表计数
    assert f"dm-story:story-1" in mem.bumped
    assert f"dm-highlights:{CODE}" in mem.bumped
    assert "dm-highlights:story-1" in mem.bumped
    assert f"dm-stories:{CODE}" in mem.bumped


def test_running_job_status_not_cached():
    """解析中的状态绝不进缓存 —— 否则前端看到的是卡住的进度。"""
    store = _FakeStore(job_status="extracting")
    mem = _MemCache()
    svc = _service(store)

    async def run() -> None:
        with _patch_cache(mem), mock.patch.object(
            store_mod, "get_dm_store", return_value=store
        ):
            await svc.get_status(_FakeScript())
            await svc.get_status(_FakeScript())

    asyncio.run(run())
    assert not mem.keys_startswith(f"dm-status:{CODE}")
    # 两次都直查数据库
    assert store.calls.count("latest_job") == 2


def test_terminal_job_status_cached():
    store = _FakeStore(job_status="completed")
    mem = _MemCache()
    svc = _service(store)

    async def run() -> None:
        with _patch_cache(mem), mock.patch.object(
            store_mod, "get_dm_store", return_value=store
        ):
            first = await svc.get_status(_FakeScript())
            second = await svc.get_status(_FakeScript())
        assert first.indexed is True
        assert second.indexed is True

    asyncio.run(run())
    assert mem.keys_startswith(f"dm-status:{CODE}")
    assert store.calls.count("latest_job") == 1


def test_content_cache_scopes_cover_all_domains():
    scopes = store_mod.content_cache_scopes(CODE)
    assert scopes == [
        f"dm-qa-titles:{CODE}",
        f"dm-stories:{CODE}",
        f"dm-synthesis:{CODE}",
        f"dm-status:{CODE}",
        f"dm-search:{CODE}",
    ]
    assert store_mod.content_cache_scopes("") == []
