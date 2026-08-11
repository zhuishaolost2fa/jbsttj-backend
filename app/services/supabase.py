"""Supabase 数据访问层。

直接对接 PostgREST / GoTrue 的 HTTP 接口，好处是全异步、无同步阻塞、依赖极轻。
后端使用 service_role key，会绕过 RLS，因此**每个查询都必须显式带上 user_id 过滤**，
这是本层的安全底线；schema.sql 里同时保留了 RLS 策略，供前端直连时使用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthError, ConfigError, DatabaseError, ValidationError

logger = logging.getLogger("app.supabase")


class SupabaseClient:
    """PostgREST 薄封装。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._client: Optional[httpx.AsyncClient] = None

    # ---------------- 生命周期 ----------------
    async def startup(self) -> None:
        s = self._settings
        if not s.supabase_url or not s.supabase_service_role_key:
            logger.warning("Supabase 未配置完整，数据库相关接口将不可用")
            return
        self._client = httpx.AsyncClient(
            base_url=s.supabase_rest_url,
            headers={
                "apikey": s.supabase_service_role_key,
                "Authorization": f"Bearer {s.supabase_service_role_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(15.0, connect=10.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise ConfigError("Supabase 客户端未初始化，请检查 SUPABASE_URL / SERVICE_ROLE_KEY")
        return self._client

    @property
    def available(self) -> bool:
        return self._client is not None

    # ---------------- 底层请求 ----------------
    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            resp = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            logger.error("Supabase 请求失败 %s %s: %s", method, path, exc)
            raise DatabaseError(f"数据库请求失败: {exc}") from exc

        if resp.status_code >= 400:
            detail: Any
            try:
                detail = resp.json()
            except Exception:  # noqa: BLE001
                detail = resp.text
            logger.error("Supabase %s %s -> %s %s", method, path, resp.status_code, detail)
            raise DatabaseError("数据库操作失败", details=detail)
        return resp

    # ---------------- CRUD ----------------
    async def select(
        self,
        table: str,
        *,
        filters: Optional[Dict[str, str]] = None,
        columns: str = "*",
        order: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"select": columns, **(filters or {})}
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = limit
        if offset:
            params["offset"] = offset
        resp = await self._request("GET", f"/{table}", params=params)
        return resp.json()

    async def select_with_count(
        self,
        table: str,
        *,
        filters: Optional[Dict[str, str]] = None,
        columns: str = "*",
        order: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        params: Dict[str, Any] = {"select": columns, "limit": limit, "offset": offset, **(filters or {})}
        if order:
            params["order"] = order
        resp = await self._request(
            "GET", f"/{table}", params=params, headers={"Prefer": "count=exact"}
        )
        total = 0
        content_range = resp.headers.get("content-range", "")
        if "/" in content_range:
            tail = content_range.split("/")[-1]
            total = int(tail) if tail.isdigit() else 0
        return resp.json(), total

    async def select_one(
        self, table: str, *, filters: Dict[str, str], columns: str = "*"
    ) -> Optional[Dict[str, Any]]:
        rows = await self.select(table, filters=filters, columns=columns, limit=1)
        return rows[0] if rows else None

    async def insert(
        self, table: str, data: Dict[str, Any] | List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        resp = await self._request(
            "POST", f"/{table}", json=data, headers={"Prefer": "return=representation"}
        )
        rows = resp.json()
        return rows[0] if isinstance(rows, list) and rows else rows

    async def upsert(
        self,
        table: str,
        data: Dict[str, Any] | List[Dict[str, Any]],
        on_conflict: str,
    ) -> List[Dict[str, Any]]:
        resp = await self._request(
            "POST",
            f"/{table}",
            json=data,
            params={"on_conflict": on_conflict},
            headers={"Prefer": "return=representation,resolution=merge-duplicates"},
        )
        rows = resp.json()
        return rows if isinstance(rows, list) else [rows]

    async def update(
        self, table: str, *, filters: Dict[str, str], data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        resp = await self._request(
            "PATCH",
            f"/{table}",
            params=filters,
            json=data,
            headers={"Prefer": "return=representation"},
        )
        rows = resp.json()
        return rows if isinstance(rows, list) else [rows]

    async def delete(self, table: str, *, filters: Dict[str, str]) -> None:
        await self._request("DELETE", f"/{table}", params=filters)

    async def ping(self) -> bool:
        if not self.available:
            return False
        try:
            await self._request("GET", "/upload_tasks", params={"select": "id", "limit": 1})
            return True
        except DatabaseError:
            return False


class SupabaseAuth:
    """GoTrue 代理：仅用于方便调试与轻量前端，正式前端建议直接用 supabase-js。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

    def _headers(self) -> Dict[str, str]:
        if not self._settings.supabase_anon_key:
            raise ConfigError("未配置 SUPABASE_ANON_KEY，无法使用内置登录接口")
        return {
            "apikey": self._settings.supabase_anon_key,
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self._settings.supabase_auth_url}{path}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
        data: Any
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {"message": resp.text}
        if resp.status_code >= 400:
            message = data.get("error_description") or data.get("msg") or data.get("message") or "认证失败"
            raise AuthError(str(message), status_code=resp.status_code if resp.status_code < 500 else 502)
        return data

    def _admin_headers(self) -> Dict[str, str]:
        if not self._settings.supabase_service_role_key:
            raise ConfigError("未配置 SUPABASE_SERVICE_ROLE_KEY，无法管理账号")
        return {
            "apikey": self._settings.supabase_service_role_key,
            "Authorization": f"Bearer {self._settings.supabase_service_role_key}",
            "Content-Type": "application/json",
        }

    async def sign_in(self, email: str, password: str) -> Dict[str, Any]:
        return await self._post("/token?grant_type=password", {"email": email, "password": password})

    async def sign_up(self, email: str, password: str) -> Dict[str, Any]:
        return await self._post("/signup", {"email": email, "password": password})

    async def refresh(self, refresh_token: str) -> Dict[str, Any]:
        return await self._post("/token?grant_type=refresh_token", {"refresh_token": refresh_token})

    async def verify_password(self, email: str, password: str) -> bool:
        """校验用户当前密码是否正确。

        用于改密 / 改邮箱前的身份确认。返回 True 表示密码正确；
        返回 False 表示凭证无效（邮箱或密码错误）；其它异常（限流、服务不可用）
        直接抛出 AuthError，由上层转成统一错误结构。
        """
        if not email:
            return False
        url = f"{self._settings.supabase_auth_url}/token?grant_type=password"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url, json={"email": email, "password": password}, headers=self._headers()
            )
        if resp.status_code == 200:
            return True
        if resp.status_code == 400:
            # 凭证无效或邮箱未验证，均视为「无法确认当前密码」
            return False
        detail: Any
        try:
            detail = resp.json()
        except Exception:  # noqa: BLE001
            detail = resp.text
        message = (
            detail.get("error_description")
            or detail.get("msg")
            or detail.get("message")
            or "校验当前密码失败"
        )
        raise AuthError(str(message), status_code=502)

    async def admin_update_user(self, user_id: str, attrs: Dict[str, Any]) -> Dict[str, Any]:
        """调用 GoTrue 管理接口更新用户属性（密码 / 邮箱等）。

        需要 service_role key，本地无直接校验密码的能力时靠它落地改密 / 改邮箱。
        """
        if not user_id:
            raise ValidationError("缺少用户标识，无法更新账号")
        url = f"{self._settings.supabase_auth_url}/admin/users/{user_id}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.put(url, json=attrs, headers=self._admin_headers())
        if resp.status_code >= 400:
            detail: Any
            try:
                detail = resp.json()
            except Exception:  # noqa: BLE001
                detail = resp.text
            message = (
                detail.get("message")
                or detail.get("error_description")
                or detail.get("msg")
                or "账号更新失败"
            )
            raise AuthError(str(message), status_code=resp.status_code if resp.status_code < 500 else 502)
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {}


supabase = SupabaseClient()
supabase_auth = SupabaseAuth()


def get_supabase() -> SupabaseClient:
    return supabase


def get_supabase_auth() -> SupabaseAuth:
    return supabase_auth
