"""临时验证脚本：用真实 Redis 跑一遍详情页缓存的读写与失效（不入库、不发网络请求）。

用法（需本机 Redis 已启动）：
    .venv/Scripts/python.exe scripts/_probe_cache_e2e.py
"""

from __future__ import annotations

import asyncio
import sys

from app.core.config import settings


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "redis://127.0.0.1:6379/1"
    settings.celery_redis_url = url

    from app.schemas.dm_guide import StoryItem, StoryListResult
    from app.services import redis_cache as cache

    scope = "dm-stories:probe-demo"
    v0 = await cache.get_scope_version(scope)
    key = f"{scope}:{v0}:-:50:0"

    payload = StoryListResult(
        script_code="probe-demo",
        script_title="探针",
        total=1,
        items=[
            StoryItem(
                id="s1",
                document_id="d1",
                script_code="probe-demo",
                story_type="timeline",
                title="开场",
                content="正文",
            )
        ],
    )

    await cache.cache_set_model(key, payload, ttl_seconds=60)
    hit = await cache.cache_get_model(key, StoryListResult)
    assert hit is not None, "写入后应能读回（Redis 不可用？）"
    assert hit.items[0].title == "开场" and hit.total == 1
    print(f"[ok] 写读一致：{hit.script_code} / {hit.items[0].title} / total={hit.total}")

    # 版本号 +1 后，旧 key 组合自然 miss（模拟解析完成后的失效）
    await cache.bump_scope_versions([scope])
    v1 = await cache.get_scope_version(scope)
    assert v1 == v0 + 1, f"版本号应 +1：{v0} -> {v1}"
    stale = await cache.cache_get_model(key, StoryListResult)
    new_key = f"{scope}:{v1}:-:50:0"
    fresh = await cache.cache_get_model(new_key, StoryListResult)
    assert fresh is None, "新版本号下应当 miss"
    print(f"[ok] bump 后 version {v0}->{v1}，旧 key 内容仍在（TTL 兜底）: {stale is not None}，新 key miss")

    # TTL 写入正确
    client = cache._get_client()
    ttl = await client.ttl(key) if client else -1
    print(f"[ok] 缓存 TTL = {ttl}s")

    # 损坏内容当 miss
    await cache.cache_set(key, "{bad json", 60)
    assert await cache.cache_get_model(key, StoryListResult) is None
    print("[ok] 缓存内容损坏时降级为 miss（不会抛异常）")

    # 批量失效
    await cache.bump_scope_versions(["dm-stories:probe-demo", "dm-synthesis:probe-demo"])
    assert await cache.get_scope_version("dm-synthesis:probe-demo") == 1
    print("[ok] 批量 bump 生效")

    # 清理探针 key
    if client:
        await client.delete(key)
    print("[done] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
