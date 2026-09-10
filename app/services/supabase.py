"""Supabase 数据访问层。

直接对接 PostgREST / GoTrue 的 HTTP 接口，好处是全异步、无同步阻塞、依赖极轻。
后端使用 service_role key，会绕过 RLS，因此**每个查询都必须显式带上 user_id 过滤**，
这是本层的安全底线；schema.sql 里同时保留了 RLS 策略，供前端直连时使用。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthError, ConfigError, DatabaseError, ValidationError

logger = logging.getLogger("app.supabase")

# /me 等接口用来缓存「实时邮箱验证状态」的短 TTL 缓存，避免每次请求都打 GoTrue。
_admin_user_cache: Dict[str, Tuple[float, bool]] = {}
_ADMIN_USER_CACHE_TTL = 30.0


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

    async def rpc(self, name: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        """调用 Postgres 函数（``POST /rpc/{name}``）。

        用于 PostgREST 表达不了的原子操作，如 `increment_script_view` 的
        ``view_count = view_count + 1`` 自增。无返回体时返回 None。
        """
        resp = await self._request("POST", f"/rpc/{name}", json=payload or {})
        if not resp.content:
            return None
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return resp.text

    async def ping(self) -> bool:
        if not self.available:
            return False
        try:
            await self._request("GET", "/upload_tasks", params={"select": "id", "limit": 1})
            return True
        except DatabaseError:
            return False


# 认证代理出网超时：宁可快速失败返回明确错误，也不要卡 15s 后冒泡成 500。
AUTH_REQUEST_TIMEOUT = 5.0

# GoTrue 有两种完全不同的 429，文案接近但解法天差地别，必须分开：
#  1) 邮件配额（error_code=over_email_send_rate_limit / "email rate limit exceeded"）
#     是**项目级**的：注册确认、找回密码、magiclink、OTP、改邮箱全部共用一个池子。
#     内置 SMTP 固定 2 封/小时且不可调 —— 只能在 Supabase 后台配自定义 SMTP 或
#     Send Email hook 才是唯一的解法（配完之后 rate_limit_email_sent 才可调）。
#  2) 重发冷却（"you can only request this once every 60 seconds"）
#     只是同一动作的防刷窗口，等 60 秒即可。
_EMAIL_QUOTA_HINTS = ("email rate limit exceeded", "over_email_send_rate_limit")
_COOLDOWN_HINTS = ("you can only request this once every",)


def _describe_rate_limit(message: str, error_code: str) -> Tuple[str, str, str]:
    """把 GoTrue 的 429 文案翻译成不改语义的中文提示。

    返回 ``(message, code, hint)``。hint 给前端做分支，不参与用户可见文案。
    """
    low = f"{message} {error_code}".lower()
    if any(h in low for h in _EMAIL_QUOTA_HINTS):
        return (
            "邮件发送额度已用尽，请稍后再试",
            "auth_email_rate_limited",
            "smtp_quota_exhausted",
        )
    if any(h in low for h in _COOLDOWN_HINTS) or "over_request_rate_limit" in low:
        return (
            "操作过于频繁，同一邮箱请间隔 60 秒后重试",
            "auth_rate_limited",
            "cooldown_60s",
        )
    return ("操作过于频繁，请稍后再试", "auth_rate_limited", "unknown")


def _parse_retry_after(raw: Optional[str]) -> Optional[int]:
    """GoTrue 的 Retry-After 有时给秒数、有时给 HTTP 日期，这里只取秒数。"""
    if not raw:
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return value if value > 0 else None


class SupabaseAuth:
    """GoTrue 代理：仅用于方便调试与轻量前端，正式前端建议直接用 supabase-js。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        # GoTrue 专用长连接池，懒创建。见 auth_client 属性的说明。
        self._auth_http: Optional[httpx.AsyncClient] = None

    @property
    def auth_client(self) -> httpx.AsyncClient:
        """GoTrue 请求复用的 client（懒创建，带 keep-alive 连接池）。

        为什么不能写 `async with httpx.AsyncClient()`：
        那会在**每次调用**时新建 TCP + TLS 连接。从国内服务器访问海外 Supabase，
        单次 TLS 握手实测约 0.7s；而这里的方法（登录 / refresh / 微信免密签发 /
        OTP / admin 用户管理）在一次业务请求里往往要打多次 GoTrue，
        新建连接的开销会远超业务本身。复用连接池后只在首次握手。

        两个前提（都已满足，改动因此不改变语义）：
          1) 所有调用方传的都是绝对 URL，httpx 会忽略 base_url；
          2) client 级 headers 只放 Content-Type，apikey / Authorization 由每次
             请求的 headers 覆盖（httpx 请求级 headers 优先级高于 client 级）。
        """
        if self._auth_http is None:
            self._auth_http = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=10.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                headers={"Content-Type": "application/json"},
            )
        return self._auth_http

    async def aclose(self) -> None:
        """释放连接池。进程级单例正常退出可不调，测试里反复建实例时应调。"""
        if self._auth_http:
            await self._auth_http.aclose()
            self._auth_http = None

    def _headers(self) -> Dict[str, str]:
        # GoTrue 只校验 apikey 是不是本项目的有效 key，anon 与 service_role 都接受。
        # 优先用 anon（权限最小）；未配置时回落到 service_role，避免 anon key
        # 缺失/失效（历史上有过占位符导致 register/login/refresh 全线 401）时
        # 整条登录链路不可用。这是服务端内部调用，key 不会下发到客户端。
        key = self._settings.supabase_anon_key or self._settings.supabase_service_role_key
        if not key:
            raise ConfigError("未配置 SUPABASE_ANON_KEY 或 SUPABASE_SERVICE_ROLE_KEY，无法使用内置登录接口")
        return {
            "apikey": key,
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self._settings.supabase_auth_url}{path}"
        try:
            resp = await self.auth_client.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=AUTH_REQUEST_TIMEOUT,  # 单独的 5s 超时，保持原语义
            )
        except httpx.HTTPError as exc:
            # 连接超时 / 被防火墙丢弃等：典型为后端出网无法抵达 supabase.co
            # （如国内网络环境）。快速失败并返回明确错误，避免卡 15s 后冒泡成 500。
            logger.error("调用 GoTrue 失败（%s）：%s", url, exc)
            raise AuthError(
                "无法连接认证服务，请检查 SUPABASE_URL 出网连通性（如国内网络访问 supabase.co 被阻断）",
                status_code=502,
            ) from exc
        data: Any
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {"message": resp.text}
        if resp.status_code == 429:
            # 429 必须原样返回，不能被当成上游故障降级成 502 —— 那是另一种故障语义，
            # 前端会误判成「服务挂了」而不是「等一会儿就好」。
            message = data.get("error_description") or data.get("msg") or data.get("message") or "操作过于频繁"
            friendly, code, hint = _describe_rate_limit(
                str(message), str(data.get("error_code") or data.get("code") or "")
            )
            retry_after = _parse_retry_after(resp.headers.get("retry-after"))
            logger.warning("GoTrue 限流（%s hint=%s retry_after=%s）", path, hint, retry_after)
            raise AuthError(
                friendly,
                code=code,
                status_code=429,
                details={"hint": hint, "retry_after": retry_after},
            )
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

    async def sign_up(self, email: str, password: str, *, redirect_to: str | None = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"email": email, "password": password}
        redirect = redirect_to or self._settings.auth_email_redirect_url
        if redirect:
            # 不传时 GoTrue 用项目后台的 Site URL（默认 localhost:3000），
            # 生产环境点开就是死链。传了还必须同时出现在后台的
            # Authentication → URL Configuration → Redirect URLs 白名单里。
            payload["email_redirect_to"] = redirect
        return await self._post("/signup", payload)

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
        resp = await self.auth_client.post(
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

    async def admin_create_user(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        """用 service_role 创建用户，并**直接标记为邮箱已验证**。

        与前端 /auth/register 的关键差异：这里必须传 email_confirm=True。
        微信用户用的是占位邮箱 wx_xxx@wechat.local，永远收不到验证邮件，
        未确认时 GoTrue 会直接拒绝 password grant，用户就再也登不进来。
        """
        url = f"{self._settings.supabase_auth_url}/admin/users"
        resp = await self.auth_client.post(url, json=attrs, headers=self._admin_headers())
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
                or "创建账号失败"
            )
            raise AuthError(str(message), status_code=resp.status_code if resp.status_code < 500 else 502)
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {}

    async def admin_update_user(self, user_id: str, attrs: Dict[str, Any]) -> Dict[str, Any]:
        """调用 GoTrue 管理接口更新用户属性（密码 / 邮箱等）。

        需要 service_role key，本地无直接校验密码的能力时靠它落地改密 / 改邮箱。
        """
        if not user_id:
            raise ValidationError("缺少用户标识，无法更新账号")
        url = f"{self._settings.supabase_auth_url}/admin/users/{user_id}"
        resp = await self.auth_client.put(url, json=attrs, headers=self._admin_headers())
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

    async def admin_get_user(self, user_id: str) -> Dict[str, Any]:
        """用 service_role 调 GoTrue 管理接口读取用户实时状态。

        用于 /me 等场景获取真实的邮箱验证状态（email_confirmed_at），
        避免依赖签发过早、claim 已过期的 access token。
        """
        if not user_id:
            raise ValidationError("缺少用户标识，无法读取账号")
        url = f"{self._settings.supabase_auth_url}/admin/users/{user_id}"
        resp = await self.auth_client.get(url, headers=self._admin_headers())
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
                or "读取账号失败"
            )
            raise AuthError(str(message), status_code=resp.status_code if resp.status_code < 500 else 502)
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {}

    # ---------------- 免密码签发会话 ----------------
    async def generate_link(self, email: str, link_type: str = "magiclink") -> Dict[str, Any]:
        """用 service_role 生成一次性登录链接（**不会真的发邮件**）。

        只接受 email，不接受 user_id（实测传 user_id 会 400
        "An email address is required"），所以调用方要先拿到用户当前邮箱。
        """
        if not email:
            raise ValidationError("缺少邮箱，无法生成登录凭证")
        url = f"{self._settings.supabase_auth_url}/admin/generate_link"
        resp = await self.auth_client.post(
            url, json={"type": link_type, "email": email}, headers=self._admin_headers()
        )
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
                or "生成登录凭证失败"
            )
            raise AuthError(str(message), status_code=resp.status_code if resp.status_code < 500 else 502)
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {}

    async def verify_link(self, token_hash: str, link_type: str) -> Dict[str, Any]:
        """用 generate_link 得到的 token_hash 直接兑换会话（服务端完成，用户无需点链接）。

        注意：新版 GoTrue 只接受 **POST + JSON body**。用 GET 带 query 参数会返回
        400 "Verify requires a token or a token hash"。
        """
        payload = {"token_hash": token_hash, "type": link_type}
        url = f"{self._settings.supabase_auth_url}/verify"
        resp = await self.auth_client.post(url, json=payload, headers=self._headers())
        if resp.status_code >= 400:
            detail: Any
            try:
                detail = resp.json()
            except Exception:  # noqa: BLE001
                detail = resp.text
            message = (
                detail.get("error_description")
                or detail.get("msg")
                or detail.get("message")
                or "凭证校验失败"
            )
            raise AuthError(str(message), status_code=resp.status_code if resp.status_code < 500 else 502)
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {}

    async def issue_session_for_user(self, user_id: str) -> Dict[str, Any]:
        """**在不知道密码的前提下**给指定用户签出一整套会话。

        这是微信登录与邮箱账号打通的关键：GoTrue 一个账号只有一个密码，
        若用「确定性密码」做 password grant，微信登录就会把用户自设的邮箱密码
        顶掉（反之亦然）。改用 magiclink 后两种登录方式各自独立、互不影响。

        等价于「以该用户身份登录」，因此**只能**在已经完成强身份验证之后调用
        —— 微信侧即 code2session 成功（openid 由微信签名保证）。

        返回结构与 sign_in / refresh 一致（access_token / refresh_token / ...）。
        """
        if not user_id:
            raise ValidationError("缺少用户标识，无法签发会话")
        user = await self.admin_get_user(user_id)
        email = str(user.get("email") or "")
        if not email:
            raise AuthError("账号缺少邮箱，无法签发会话", status_code=502)
        link = await self.generate_link(email, "magiclink")
        token_hash = link.get("hashed_token")
        if not token_hash:
            # 老版本 GoTrue 不返回 hashed_token，需要从 action_link 里解析
            from urllib.parse import urlparse, parse_qs

            qs = parse_qs(urlparse(str(link.get("action_link") or "")).query)
            token_hash = (qs.get("token_hash") or qs.get("token") or [""])[0]
        if not token_hash:
            raise AuthError("生成登录凭证失败", status_code=502)
        return await self.verify_link(token_hash, "magiclink")

    async def find_user_id_by_email(
        self, email: str, per_page: int = 200
    ) -> str:
        """按邮箱反查 user_id；找不到返回空串（**不抛异常**）。

        GoTrue 管理接口的 listUsers 不支持按 email 精确过滤，只能分页遍历。
        调用方都在低频路径上（建号报「邮箱已存在」的补救、绑定邮箱的占用校验），
        可接受。真要做成高频查询，应在业务表里维护 email → user_id 的映射。

        终止条件只看「本页是否满」：**不能依赖响应里的 total 字段** ——
        实测该字段为 None，若写成 `page * per_page >= total`，满页时会立刻
        break，用户数超过一页后后面的账号全部漏判。
        """
        if not email:
            return ""
        url = f"{self._settings.supabase_auth_url}/admin/users"
        target = email.strip().lower()
        page = 1
        # 复用 auth_client：翻页时多个请求共享同一条 keep-alive 连接
        while page <= 100:  # 上限兜底，避免异常情况下无限翻页
            resp = await self.auth_client.get(
                url,
                headers=self._admin_headers(),
                params={"page": str(page), "per_page": str(per_page)},
            )
            if resp.status_code >= 400:
                break
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                break
            users = payload.get("users") or []
            for item in users:
                if str(item.get("email") or "").strip().lower() == target:
                    return str(item.get("id") or "")
            # 本页不满即已到最后一页；不依赖 total（实测为 None）
            if len(users) < per_page:
                break
            page += 1
        return ""

    # ---------------- 邮箱验证码（OTP）----------------
    async def send_email_otp(self, email: str) -> None:
        """发送 6 位邮箱验证码。create_user=False：邮箱未注册时不建号。

        Supabase 内置 SMTP 限流很紧（实测约 60 秒 1 封，返回 429
        over_email_send_rate_limit）。生产建议在 Supabase 后台配自定义 SMTP。
        """
        payload = {"email": email, "create_user": False}
        url = f"{self._settings.supabase_auth_url}/otp"
        resp = await self.auth_client.post(url, json=payload, headers=self._headers())
        if resp.status_code >= 400:
            detail: Any
            try:
                detail = resp.json()
            except Exception:  # noqa: BLE001
                detail = resp.text
            message = (
                detail.get("error_description")
                or detail.get("msg")
                or detail.get("message")
                or "发送验证码失败"
            )
            raise AuthError(str(message), status_code=resp.status_code)

    async def verify_email_otp(
        self, email: str, token: str, *, verify_type: str = "email"
    ) -> Dict[str, Any]:
        """校验 6 位邮箱验证码，成功返回会话。

        verify_type 决定这次兑换的语义：signup / recovery / magiclink /
        email_change / email，必须与发验证码时的场景一致，否则 GoTrue 会拒绝。
        """
        payload = {"email": email, "token": token, "type": verify_type}
        url = f"{self._settings.supabase_auth_url}/verify"
        resp = await self.auth_client.post(url, json=payload, headers=self._headers())
        if resp.status_code >= 400:
            detail: Any
            try:
                detail = resp.json()
            except Exception:  # noqa: BLE001
                detail = resp.text
            message = (
                detail.get("error_description")
                or detail.get("msg")
                or detail.get("message")
                or "验证码错误或已过期"
            )
            raise AuthError(str(message), status_code=resp.status_code)
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {}

    async def get_email_verified(self, user_id: str) -> bool:
        """返回用户实时邮箱是否已验证，带 30s TTL 缓存。

        优先读取 GoTrue 管理接口的 email_confirmed_at / confirmed_at；
        若接口不可用（未配置 service_role 或网络异常）则退化为 False，
        由调用方用 token claim 兜底。
        """
        now = time.monotonic()
        cached = _admin_user_cache.get(user_id)
        if cached is not None:
            ts, value = cached
            if now - ts < _ADMIN_USER_CACHE_TTL:
                return value
        try:
            user = await self.admin_get_user(user_id)
        except (AuthError, ConfigError) as exc:  # noqa: BLE001
            logger.warning("读取用户实时状态失败（id=%s），退回未验证: %s", user_id, exc)
            return False
        confirmed = bool(user.get("email_confirmed_at") or user.get("confirmed_at"))
        _admin_user_cache[user_id] = (now, confirmed)
        return confirmed


supabase = SupabaseClient()
supabase_auth = SupabaseAuth()


def get_supabase() -> SupabaseClient:
    return supabase


def get_supabase_auth() -> SupabaseAuth:
    return supabase_auth
