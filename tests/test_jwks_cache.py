"""JWKSCache 的拉取重试与失败恢复。

场景来源：线上遇到过一次拉取 JWKS 撞满 10 秒超时，表现是「第一次请求 401，
刷新一下又好了」。单次网络抖动的概率不容忽视，缓存层必须能自己扛过去。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from app.core.exceptions import AuthError
from app.core.security import JWKSCache


class FakeResponse:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Dict[str, Any]:
        return self._payload


class FlakyClient:
    """前 fail_times 次请求抛异常，之后正常返回。"""

    def __init__(self, payload: Dict[str, Any], fail_times: int) -> None:
        self.payload = payload
        self.fail_times = fail_times
        self.calls: List[int] = []

    async def __aenter__(self) -> "FlakyClient":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def get(self, url: str) -> FakeResponse:
        self.calls.append(1)
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("网络抖动")
        return FakeResponse(self.payload)


JWKS = {
    "keys": [
        {
            "kty": "EC",
            "kid": "kid-1",
            "alg": "ES256",
            "crv": "P-256",
            "x": "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU",
            "y": "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0",
            "use": "sig",
        }
    ]
}


async def _noop_sleep(*_args: Any, **_kwargs: Any) -> None:
    """跳过重试等待，让测试不用真等 0.4 秒。"""
    return None


def _patch(monkeypatch: pytest.MonkeyPatch, client: FlakyClient) -> None:
    monkeypatch.setattr(
        "app.core.security.httpx.AsyncClient", lambda *a, **kw: client
    )
    monkeypatch.setattr("app.core.security.asyncio.sleep", _noop_sleep)


def test_first_fetch_failure_is_retried(monkeypatch: pytest.MonkeyPatch):
    client = FlakyClient(JWKS, fail_times=1)
    _patch(monkeypatch, client)

    cache = JWKSCache("https://example.invalid/jwks.json")
    key, alg = asyncio.run(cache.get("kid-1"))

    assert key is not None
    assert alg == "ES256"
    assert len(client.calls) == 2, "第一次失败后应重试一次，而不是直接 401"


def test_persistent_failure_raises_jwks_unavailable(monkeypatch: pytest.MonkeyPatch):
    client = FlakyClient(JWKS, fail_times=99)
    _patch(monkeypatch, client)

    cache = JWKSCache("https://example.invalid/jwks.json")
    with pytest.raises(AuthError) as exc:
        asyncio.run(cache.get("kid-1"))

    assert exc.value.code == "jwks_unavailable"
    assert len(client.calls) == 2, "重试次数上限定为 2（原请求 + 1 次重试）"


def test_failed_fetch_is_not_cached(monkeypatch: pytest.MonkeyPatch):
    """一次失败不能被固化成长时间故障：下次请求必须重新去拉。"""
    client = FlakyClient(JWKS, fail_times=99)
    _patch(monkeypatch, client)

    cache = JWKSCache("https://example.invalid/jwks.json")
    for _ in range(2):
        with pytest.raises(AuthError):
            asyncio.run(cache.get("kid-1"))

    assert len(client.calls) == 4, "每次请求都应重新尝试拉取，而不是复用失败状态"


def test_successful_fetch_is_cached(monkeypatch: pytest.MonkeyPatch):
    client = FlakyClient(JWKS, fail_times=0)
    _patch(monkeypatch, client)

    cache = JWKSCache("https://example.invalid/jwks.json")
    asyncio.run(cache.get("kid-1"))
    asyncio.run(cache.get("kid-1"))

    assert len(client.calls) == 1, "TTL 内不应重复拉取"
