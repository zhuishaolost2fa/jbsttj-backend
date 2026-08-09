"""用户鉴权：校验 Supabase 签发的 JWT。

Supabase 目前存在两代密钥体系，这里都做了兼容：
  1. 传统对称密钥 HS256 —— 用 Project Settings 里的 JWT Secret 本地校验；
  2. 新版非对称密钥 ES256 / RS256 —— 从 /auth/v1/.well-known/jwks.json 拉取公钥校验，
     公钥结果带 TTL 缓存，遇到未知 kid 会主动刷新一次（应对密钥轮换）。

另外提供一条可选的服务间调用通道：请求头带 X-API-Key，
并用 X-User-Id 指明代表哪个用户操作，便于内部服务或定时任务调用。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthError, ConfigError

logger = logging.getLogger("app.security")

bearer_scheme = HTTPBearer(auto_error=False, description="Supabase access token")


class CurrentUser(BaseModel):
    """经过校验的调用者身份。"""

    id: str
    email: Optional[str] = None
    role: str = "authenticated"
    is_service: bool = False
    claims: Dict[str, Any] = {}

    @property
    def is_admin(self) -> bool:
        app_meta = self.claims.get("app_metadata") or {}
        return self.role in {"service_role", "admin"} or app_meta.get("role") == "admin"


class JWKSCache:
    """带 TTL 的 JWKS 缓存，避免每次请求都打一次 Supabase。"""

    def __init__(self, url: str, ttl: int = 600) -> None:
        self._url = url
        self._ttl = ttl
        self._keys: Dict[str, Any] = {}
        self._algs: Dict[str, str] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(self._url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("拉取 JWKS 失败: %s", exc)
            raise AuthError("无法获取签名公钥，token 校验不可用", code="jwks_unavailable") from exc

        keys, algs = {}, {}
        for item in data.get("keys", []):
            kid = item.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = jwt.PyJWK.from_dict(item).key
                algs[kid] = item.get("alg", "ES256")
            except Exception as exc:  # noqa: BLE001
                logger.warning("跳过无法解析的 JWK %s: %s", kid, exc)
        self._keys, self._algs = keys, algs
        self._fetched_at = time.monotonic()

    async def get(self, kid: str) -> tuple[Any, str]:
        async with self._lock:
            expired = time.monotonic() - self._fetched_at > self._ttl
            if expired or kid not in self._keys:
                await self._refresh()
            if kid not in self._keys:
                raise AuthError("token 使用了未知的签名密钥", code="unknown_kid")
            return self._keys[kid], self._algs.get(kid, "ES256")


class JWTVerifier:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._jwks = JWKSCache(settings.jwks_url) if settings.jwks_url else None

    async def verify(self, token: str) -> Dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError("token 格式非法", code="invalid_token") from exc

        alg = (header.get("alg") or "").upper()
        key: Any
        algorithms: List[str]

        if alg == "HS256":
            if not self._settings.supabase_jwt_secret:
                raise ConfigError("未配置 SUPABASE_JWT_SECRET，无法校验 HS256 token")
            key, algorithms = self._settings.supabase_jwt_secret, ["HS256"]
        else:
            if not self._jwks:
                raise ConfigError("未配置 JWKS 地址，无法校验非对称签名 token")
            kid = header.get("kid")
            if not kid:
                raise AuthError("token 缺少 kid", code="invalid_token")
            key, key_alg = await self._jwks.get(kid)
            algorithms = [key_alg or alg or "ES256"]

        audience = self._settings.supabase_jwt_audience or None
        try:
            return jwt.decode(
                token,
                key=key,
                algorithms=algorithms,
                audience=audience,
                options={
                    "verify_aud": bool(audience),
                    "require": ["exp", "sub"],
                },
                leeway=30,
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("登录已过期，请重新登录", code="token_expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError("token 受众不匹配", code="invalid_audience") from exc
        except jwt.PyJWTError as exc:
            raise AuthError(f"token 校验失败: {exc}", code="invalid_token") from exc


_verifier: Optional[JWTVerifier] = None


def get_verifier() -> JWTVerifier:
    global _verifier
    if _verifier is None:
        _verifier = JWTVerifier(get_settings())
    return _verifier


def _service_identity(request: Request, settings: Settings) -> Optional[CurrentUser]:
    api_key = request.headers.get("X-API-Key")
    if not api_key or not settings.service_api_key:
        return None
    # 固定时长比较，避免时序侧信道
    import hmac

    if not hmac.compare_digest(api_key, settings.service_api_key):
        raise AuthError("service api key 无效", code="invalid_api_key")
    on_behalf = request.headers.get("X-User-Id")
    if not on_behalf:
        raise AuthError("服务调用需要通过 X-User-Id 指定目标用户", code="missing_user_id")
    return CurrentUser(id=on_behalf, role="service_role", is_service=True)


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    """强制鉴权依赖：挂在需要登录的路由上。"""
    service_user = _service_identity(request, settings)
    if service_user:
        return service_user

    if credentials is None or not credentials.credentials:
        raise AuthError("缺少 Authorization: Bearer <token>", code="missing_token")

    claims = await get_verifier().verify(credentials.credentials)
    subject = claims.get("sub")
    if not subject:
        raise AuthError("token 缺少 sub 字段", code="invalid_token")

    return CurrentUser(
        id=str(subject),
        email=claims.get("email"),
        role=str(claims.get("role") or "authenticated"),
        claims=claims,
    )


async def get_current_user_optional(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> Optional[CurrentUser]:
    """可选鉴权：匿名也放行，用于公开资源。"""
    try:
        return await get_current_user(request, credentials, settings)
    except (AuthError, ConfigError):
        return None
