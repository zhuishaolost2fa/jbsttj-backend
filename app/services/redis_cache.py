"""Redis 缓存工具（cache-aside 模式）。

供剧本列表等**公开读接口**做结果缓存：

- 客户端延迟初始化 + 失败降级：Redis 不可用时直查数据库，绝不因为缓存挂了拖垮接口；
  失败后进入 30s 冷却窗口，期间直接跳过缓存，避免每次请求都尝试重连；
- 版本号失效：写操作（新增 / 修改 / 下架）后 ``bump_version()`` 让旧缓存 key 整体失效，
  无需逐条扫描删除 —— 读缓存前先取版本号拼进 key，版本一变自然 miss；
- 小数值变化（如浏览量 +1）不 bump 版本号，靠 TTL 自然过期，避免高频写把缓存打穿。

连接复用 ``CELERY_REDIS_URL``（去重指纹所在实例），不新增任何凭据配置。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from app.core.config import settings

logger = logging.getLogger("app.cache")

# 所有缓存 key 统一挂这个前缀，便于运维按前缀排查 / 清空
_KEY_PREFIX = "jbs:cache"
# 全局版本号：写操作 INCR 一次，列表缓存 key 里带上它
_VERSION_KEY = f"{_KEY_PREFIX}:version"
# Redis 失败后的冷却窗口：期间不再尝试连接，直查数据库
_FALLBACK_SECONDS = 30.0

_client: Any = None
_last_fail: float = 0.0


def _mark_failed() -> None:
    global _client, _last_fail
    _client = None
    _last_fail = time.monotonic()


def _get_client() -> Any:
    """延迟初始化异步 Redis 客户端；不可用返回 None（调用方降级直查）。"""
    global _client
    if _client is not None:
        return _client
    if time.monotonic() - _last_fail < _FALLBACK_SECONDS:
        return None
    url = settings.celery_redis_url
    if not url:
        return None
    try:
        import redis  # 延迟导入：未装 redis 包时列表接口仍可直查数据库

        # from_url 惰性建连，连接问题会在首次 get/set 时暴露并被捕获降级
        _client = redis.asyncio.from_url(url, decode_responses=True)
        return _client
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 缓存不可用，列表接口直查数据库: %s", exc)
        _mark_failed()
        return None


async def cache_get(key: str) -> Optional[str]:
    """读缓存；任何异常都降级为 None（miss 直查数据库）。"""
    client = _get_client()
    if client is None:
        return None
    try:
        return await client.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 缓存读取失败，直查数据库: %s", exc)
        _mark_failed()
        return None


async def cache_set(key: str, value: str, ttl_seconds: int) -> None:
    """写缓存；失败静默（下一请求 miss 后重新回填）。"""
    client = _get_client()
    if client is None:
        return
    try:
        await client.set(key, value, ex=ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 缓存写入失败: %s", exc)
        _mark_failed()


async def get_version() -> int:
    """当前缓存版本号；读不到 / 缓存不可用时按 0 处理（key 归零重建，无副作用）。"""
    client = _get_client()
    if client is None:
        return 0
    try:
        raw = await client.get(_VERSION_KEY)
        return int(raw) if raw else 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 缓存版本号读取失败: %s", exc)
        _mark_failed()
        return 0


async def bump_version() -> None:
    """写操作后调用：版本号 +1，所有旧列表缓存 key 立即失效。失败静默。"""
    client = _get_client()
    if client is None:
        return
    try:
        await client.incr(_VERSION_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 缓存版本号自增失败: %s", exc)
        _mark_failed()
