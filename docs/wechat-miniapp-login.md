# 微信小程序登录改造方案

> 目标：在小程序端实现「微信一键登录」，且**现有业务代码零改动**。
> 后端 `docs/` 目录，前端 `C:\Applications\works\jbsttj-frontend`。

**落地状态**：后端与前端**均已实现**（第 3、4 节代码与仓库逐行一致）。

- 后端：全量 `pytest` 84 passed（79 原有 + 5 新增微信登录单测）。
- 前端：`tsc --noEmit` 在 `src/` 下 0 报错；`build:h5` 与 `build:weapp` 均构建成功。
- 尚未联调：只剩第 6 节的 **第 3、4 项**（换真实 AppID、配 request 合法域名）。
  第 1、2 项（执行 SQL、填微信凭证）经实测**已完成**。

> 手机号绑定**不做**（需企业主体 + 认证 + 付费），后端未实现
> `getuserphonenumber`，前端也无对应入口。

---

## 0. 结论先行

**核心难点不是「调微信接口」，而是「微信用户怎么变成你现有的 Supabase 用户」。**

你现有的全部业务（`script_requests`、`profiles`、DM 手册、OSS 上传）都以
`CurrentUser.id` 为准，而它来自 Supabase JWT 的 `sub`（`auth.users.id`，UUID）。
微信只有 openid，没有账号体系。所以必须做一层身份映射。

**推荐方案：`user_identities` 映射表 + GoTrue 托管账号 + password grant 换真 token。**

| | 做法 | 评价 |
|---|---|---|
| A 自建映射 + 自签 JWT | 用 `SUPABASE_JWT_SECRET` 自己签 HS256 token | 快，但 `/auth/refresh` 要自己实现；若项目是新版 ES256 密钥则根本签不了 |
| **B 映射表 + GoTrue password grant** ✅ | admin 建一个占位邮箱账号 → 用确定性密码登录 → 拿**真正的** GoTrue token | `/refresh` 直接复用、业务代码零改动、登录后与邮箱用户完全等价 |
| C Supabase 原生第三方登录 | 官方 Sign in with WeChat | Supabase 不原生支持微信，需要自建 OAuth IdP，成本高 |

**选 B。** 登录成功后返回的 `TokenResponse` 与 `POST /auth/login` **结构完全一致**，
前端 `tokenManager.setSession()` 照旧，续期、401 拦截、退出登录全部不用动。

---

## 1. 架构与时序

### 1.1 首次登录（用户第一次进小程序）

```
小程序                     后端 /api/v1/auth/wechat/login             微信             Supabase GoTrue
  │                                   │                                 │                    │
  ├─ Taro.login() ──► code            │                                 │                    │
  ├─ POST {code, nickname, avatarUrl}►│                                 │                    │
  │                                   ├─ sns/jscode2session ───────────►│                    │
  │                                   │◄── { openid, unionid, session_key }                  │
  │                                   │                                                      │
  │                                   ├─ 查 user_identities (wechat, openid) → 未命中        │
  │                                   ├─ POST /auth/v1/admin/users ───────────────────────►│
  │                                   │   (占位邮箱 + 确定性密码 + email_confirm=true)       │
  │                                   │◄──────── { id: <uuid> } ───────────────────────────│
  │                                   ├─ INSERT user_identities / UPSERT profiles           │
  │                                   ├─ POST /token?grant_type=password ─────────────────►│
  │                                   │◄──────── { access_token, refresh_token } ──────────│
  │◄── TokenResponse ─────────────────┤                                                      │
  ├─ tokenManager.setSession()        │                                                      │
```

### 1.2 再次登录（已有绑定）

```
Taro.login() → code → POST /auth/wechat/login
    → code2session → openid
    → 命中 user_identities → 直接 password grant
    → TokenResponse
```

只有 2 次外部 HTTP（code2session + token），没有多余开销。

### 1.3 「确定性密码」是什么

微信用户没有密码，但 GoTrue 的 password grant 必须要密码。做法是**用一个从 openid
派生的、可复现的密码**：

```python
password = "Jbs!wx" + HMAC_SHA256(WECHAT_LINK_SECRET, openid).hexdigest()[:24]
```

- 每次登录都能现算出来，**不需要存密码**；
- 30 位、含大小写/数字/符号，满足 Supabase 密码强度要求；
- 只在服务端存在，永不对外暴露；
- 极端情况（用户账号密码被 admin 改过）导致 grant 失败时，用
  `admin_update_user` 把它重置回这个密码再重试一次即可。

---

## 2. 数据模型

已生成：`sql/wechat_auth.sql`（在 Supabase SQL Editor 整段执行，幂等）。

```sql
create table public.user_identities (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null,               -- auth.users(id)
    provider     text not null,               -- 'wechat'
    provider_uid text not null,               -- openid
    union_id     text,                        -- 多端打通用
    session_key  text,                        -- 服务端专用，绝不下发
    raw          jsonb,
    created_at   timestamptz not null default now(),
    last_login_at timestamptz,
    constraint uq_user_identities unique (provider, provider_uid)
);
```

另外给 `profiles` 加一列 `provider`，让前端知道「这个用户是微信来的」。

> **为什么不建外键到 `auth.users`**：和 `profiles` / `upload_tasks` 保持一致 ——
> 跨 schema 外键会让 `service_role` 写入受限，且项目允许服务间通道写入任意 user_id。

---

## 3. 后端改造

改动 5 个文件，全是**新增**，不触碰任何现有业务逻辑。

| 文件 | 改动 |
|---|---|
| `app/core/config.py` | 新增 3 个配置项 |
| `app/services/wechat.py` | **新建**，微信接口封装 |
| `app/services/supabase.py` | `SupabaseAuth` 加 `admin_create_user` |
| `app/schemas/auth.py` | 加请求模型 + `ProfileResponse.provider` |
| `app/api/v1/auth.py` | 加 1 个端点 |

> 后端代码已落地（2026-09-03）。以下代码块与仓库现状一致，可直接作为实现说明阅读。
> 前端改造（第 4 节）尚未落地。

### 3.1 `app/core/config.py`

在「Supabase」配置块后面追加：

```python
    # ---------------- 微信小程序登录 ----------------
    wechat_appid: str = ""
    wechat_app_secret: str = ""
    # 派生「微信用户 → GoTrue 账号」确定性密码的 HMAC 密钥。
    # 留空时回落到 SUPABASE_JWT_SECRET。改动它会导致所有微信用户无法登录
    # （密码变了），所以上线后不要动。
    wechat_link_secret: str = ""

    @property
    def wechat_login_enabled(self) -> bool:
        return bool(self.wechat_appid and self.wechat_app_secret and self.supabase_service_role_key)

    @property
    def _wechat_link_key(self) -> str:
        return self.wechat_link_secret or self.supabase_jwt_secret
```

`missing_required()` **不要**加微信项 —— 微信登录是可选能力，H5 端没配也得起得来。

### 3.2 `app/services/wechat.py`（新建）

```python
"""微信小程序服务端接口。

只依赖 httpx，不引第三方 SDK。登录链路上的外部调用必须**短超时 + 明确报错**，
否则微信接口一慢会直接拖垮 /auth/wechat/login。

本模块刻意只保留登录必需的能力：
  - code2session：wx.login 的 code 换 openid / unionid / session_key
  - placeholder_email / derive_password：把 openid 映射成 GoTrue 账号所需材料

没有实现 getuserphonenumber：它要求企业主体 + 认证 + 单独付费，个人主体用不了，
且本项目不做手机号绑定，引进来只是死代码。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Dict, Optional

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthError, ConfigError, ValidationError

logger = logging.getLogger("app.wechat")

CODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"

# 微信错误码 → 用户能看懂的中文。没覆盖到的用 errmsg 兜底。
_ERR_TEXT: Dict[int, str] = {
    40029: "登录凭证无效，请重试",
    45011: "操作过于频繁，请稍后再试",
    40226: "账号已被限制登录",
    -1: "微信服务暂时不可用，请稍后重试",
}


class WeChatService:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return bool(self._settings.wechat_appid and self._settings.wechat_app_secret)

    def _require(self) -> None:
        if not self.enabled:
            raise ConfigError("未配置 WECHAT_APPID / WECHAT_APP_SECRET，微信登录不可用")

    def derive_password(self, openid: str) -> str:
        """从 openid 派生可复现的登录密码（不落库，现算现用）。"""
        key = self._settings._wechat_link_key
        if not key:
            raise ConfigError("未配置 WECHAT_LINK_SECRET 或 SUPABASE_JWT_SECRET，无法派生微信账号密码")
        digest = hmac.new(key.encode("utf-8"), openid.encode("utf-8"), hashlib.sha256).hexdigest()
        return "Jbs!wx" + digest[:24]

    @staticmethod
    def placeholder_email(openid: str) -> str:
        """微信用户的占位邮箱。用哈希而非 openid 原值，避免 openid 经 /auth/me 泄露。"""
        tail = hashlib.sha256(openid.encode("utf-8")).hexdigest()[:20]
        return f"wx_{tail}@wechat.local"

    # ---------------- 登录 ----------------
    async def code2session(self, code: str) -> Dict[str, Any]:
        """用 wx.login 的 code 换 openid / unionid / session_key。

        注意：code 一次性、5 分钟有效；并发用同一个 code 会有一个失败。
        """
        self._require()
        params = {
            "appid": self._settings.wechat_appid,
            "secret": self._settings.wechat_app_secret,
            "js_code": code,
            "grant_type": "authorization_code",
        }
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(CODE2SESSION_URL, params=params)
        except httpx.HTTPError as exc:
            logger.error("code2session 请求失败: %s", exc)
            raise AuthError("无法连接微信服务，请检查服务器出网连通性", status_code=502) from exc

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            raise AuthError("微信返回了非预期内容", status_code=502) from None

        errcode = int(data.get("errcode") or 0)
        if errcode:
            text = _ERR_TEXT.get(errcode) or data.get("errmsg") or "微信登录失败"
            logger.warning("code2session 失败: %s %s", errcode, data.get("errmsg"))
            raise ValidationError(text, code=f"wechat_{errcode}")
        if not data.get("openid"):
            logger.error("code2session 未返回 openid: %s", data)
            raise AuthError("微信未返回 openid，登录失败", status_code=502)
        return data

_wechat: Optional[WeChatService] = None


def get_wechat_service() -> WeChatService:
    global _wechat
    if _wechat is None:
        _wechat = WeChatService()
    return _wechat
```

### 3.3 `app/services/supabase.py`

在 `SupabaseAuth` 里加一个方法（复用已有的 `_admin_headers`）：

```python
    async def admin_create_user(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        """用 service_role 创建用户并**直接标记为已验证**。

        与前端 /auth/register 的区别：这里必须传 email_confirm=True，
        否则占位邮箱永远收不到验证邮件，password grant 会被 GoTrue 拒绝。
        """
        url = f"{self._settings.supabase_auth_url}/admin/users"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=attrs, headers=self._admin_headers())
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
```

### 3.4 `app/schemas/auth.py`

```python
class WechatLoginRequest(BaseModel):
    """微信小程序登录：wx.login() 拿到的 code。

    nickname / avatar_url 可选 —— 只在首次创建资料时写入，已存在则不覆盖，
    避免用户改过昵称后每次登录都被重置。
    """

    code: str = Field(min_length=1, max_length=256)
    nickname: Optional[str] = Field(default=None, max_length=30)
    avatar_url: Optional[str] = Field(default=None, max_length=1024)
```

（不做手机号绑定，因此没有 `WechatPhoneRequest`。）

`ProfileResponse` 加一个字段：

```python
class ProfileResponse(BaseModel):
    # ... 现有字段 ...
    provider: Optional[str] = None   # None=邮箱注册；'wechat'=微信登录
```

### 3.5 `app/api/v1/auth.py`

追加一个端点，并在 `_profile_response` 里补 provider：

```python
from app.core.exceptions import (
    AuthError,
    ConfigError,   # 新增
    ConflictError,
    DatabaseError,
    ValidationError,
)
from app.schemas.auth import (
    # ... 现有 import ...
    WechatLoginRequest,   # 新增
)
from app.services.wechat import WeChatService, get_wechat_service   # 新增
```

```python
def _profile_response(
    user: CurrentUser,
    profile: Optional[Dict[str, Any]],
    email_verified: bool,
) -> ProfileResponse:
    """把鉴权身份与 profiles 行拼成统一的 ProfileResponse。"""
    meta = user.claims.get("user_metadata") or {}
    # 优先用 profiles.provider（自己写的、可控），token 里的 user_metadata 兜底
    provider = (profile or {}).get("provider") or meta.get("provider")
    return ProfileResponse(
        # ... 现有字段 ...
        provider=provider,
    )
```

```python
# GoTrue 在邮箱被占用时返回 422 + 这类文案；用来识别「占位邮箱账号已存在」
# 的恢复分支（例如上次建号成功但绑定表写入失败）。
_EMAIL_EXISTS_HINTS = ("already registered", "already exists", "email_exists", "user_already_exists")


@router.post("/wechat/login", response_model=TokenResponse, summary="微信小程序一键登录")
async def wechat_login(
    payload: WechatLoginRequest,
    db: SupabaseClient = Depends(get_supabase),
    auth: SupabaseAuth = Depends(get_supabase_auth),
    wx: WeChatService = Depends(get_wechat_service),
) -> TokenResponse:
    """用 wx.login 的 code 换取与 /auth/login **完全同构**的 TokenResponse。

    链路：code → openid → 查绑定 → 未绑定则建 GoTrue 账号 → password grant。
    返回的 token 由 GoTrue 签发，可直接用现有 /auth/refresh 续期，业务侧
    （profiles / RLS / CurrentUser.id）零改动。

    安全性：本接口不校验登录态（与 /auth/login 一致），安全性完全依赖 code
    的一次性 —— 只有持有小程序 appsecret 的服务端才能兑换成功。
    """
    if not wx.enabled:
        raise ConfigError("服务端未配置微信小程序凭证，微信登录不可用")
    if not db.available:
        raise DatabaseError("数据库未配置，无法完成微信登录", code="db_unavailable")

    session = await wx.code2session(payload.code)
    openid = str(session["openid"])
    unionid = session.get("unionid")
    email = wx.placeholder_email(openid)
    password = wx.derive_password(openid)

    identity = await db.select_one(
        "user_identities",
        filters={"provider": "eq.wechat", "provider_uid": f"eq.{openid}"},
    )

    if identity and identity.get("user_id"):
        user_id = str(identity["user_id"])
    else:
        user_id = await _provision_wechat_user(
            db=db,
            auth=auth,
            wx=wx,
            openid=openid,
            unionid=unionid,
            email=email,
            password=password,
            session=session,
            nickname=payload.nickname,
            avatar_url=payload.avatar_url,
        )

    # password grant：拿真正的 GoTrue token
    try:
        data = await auth.sign_in(email, password)
    except AuthError as exc:
        if exc.status_code != 400:
            raise
        # 密码被外部改过（人工重置、账号重建）→ 用 admin 重置回确定性密码再试一次
        logger.warning("微信用户 password grant 失败，重置密码后重试（openid=%s）", openid[:8] + "***")
        await auth.admin_update_user(user_id, {"password": password})
        data = await auth.sign_in(email, password)

    # 回写登录时间，失败不影响登录结果
    try:
        await db.update(
            "user_identities",
            filters={"provider": "eq.wechat", "provider_uid": f"eq.{openid}"},
            data={"last_login_at": "now()"},
        )
    except DatabaseError:  # noqa: BLE001
        logger.warning("回写微信登录时间失败（openid=%s）", openid[:8] + "***")

    logger.info("微信登录成功（user_id=%s）", user_id)
    return _to_token(data)


async def _provision_wechat_user(
    *,
    db: SupabaseClient,
    auth: SupabaseAuth,
    wx: WeChatService,
    openid: str,
    unionid: Optional[str],
    email: str,
    password: str,
    session: Dict[str, Any],
    nickname: Optional[str],
    avatar_url: Optional[str],
) -> str:
    """首次登录：建 GoTrue 账号 + 写绑定表 + 播种 profiles，返回 user_id。"""
    try:
        created = await auth.admin_create_user(
            {
                "email": email,
                "password": password,
                # 占位邮箱永远收不到验证邮件，不预确认会导致 password grant 被拒
                "email_confirm": True,
                "user_metadata": {
                    "provider": "wechat",
                    "openid": openid,
                    "unionid": unionid,
                    "nickname": nickname,
                    "avatar_url": avatar_url,
                },
            }
        )
        user_id = str(created.get("id") or "")
    except AuthError as exc:
        hint = f"{exc.message} {exc.code} {exc.details or ''}".lower()
        if not any(h in hint for h in _EMAIL_EXISTS_HINTS):
            raise
        # 账号已存在（上次建号成功但绑定表没写进去）→ 用确定性密码登录反查 id。
        # 避免一次网络抖动就把这个微信用户永久锁死在「建号失败」。
        logger.warning("占位邮箱账号已存在，改用登录反查 user_id（openid=%s）", openid[:8] + "***")
        data = await auth.sign_in(email, password)
        user_id = str((data.get("user") or {}).get("id") or "")

    if not user_id:
        raise AuthError("创建微信账号失败", status_code=502)

    await db.upsert(
        "user_identities",
        {
            "user_id": user_id,
            "provider": "wechat",
            "provider_uid": openid,
            "union_id": unionid,
            "session_key": session.get("session_key"),
            "session_key_updated_at": "now()",
            # 只存去掉 session_key 后的快照，排障够用且不重复存敏感值
            "raw": {k: v for k, v in session.items() if k != "session_key"},
        },
        on_conflict="provider,provider_uid",
    )
    await _seed_profile(db, user_id, nickname, avatar_url)
    logger.info("微信用户首次登录（user_id=%s, openid=%s）", user_id, openid[:8] + "***")
    return user_id


async def _seed_profile(
    db: SupabaseClient,
    user_id: str,
    nickname: Optional[str],
    avatar_url: Optional[str],
) -> None:
    """首次登录播种 profiles。

    已有资料时只补 provider，**不覆盖**用户自己改过的昵称 / 头像 ——
    否则每次重新登录都会把改名冲掉。
    """
    existing = await db.select_one("profiles", filters={"id": f"eq.{user_id}"})
    if existing is None:
        seed: Dict[str, Any] = {"id": user_id, "provider": "wechat"}
        if nickname:
            seed["nickname"] = nickname
        if avatar_url:
            seed["avatar_url"] = avatar_url
        await db.upsert("profiles", seed, on_conflict="id")
    elif not existing.get("provider"):
        await db.update("profiles", filters={"id": f"eq.{user_id}"}, data={"provider": "wechat"})
```

> 不做手机号绑定：需要企业主体 + 认证 + 单独付费，且当前业务不需要。
> 因此没有 `/auth/wechat/phone` 端点，也没有 `WechatPhoneRequest`。

### 3.6 环境变量

`.env` / Railway 各加：

```ini
WECHAT_APPID=wxXXXXXXXXXXXXXXXX
WECHAT_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# 可选；留空则回落到 SUPABASE_JWT_SECRET。上线后不要改。
WECHAT_LINK_SECRET=
```

---

## 4. 前端改造

> 本节代码**已落地**到 `jbsttj-frontend`，与 `src/` 下的实际实现逐行一致。
> 验收：`tsc --noEmit` 在 `src/` 下 0 报错；`build:h5` 与 `build:weapp` 均成功。

改动 7 个文件，核心是**新增一个登录方式**，现有邮箱登录完全保留（H5 端继续用）。

| 文件 | 改动 |
|---|---|
| `src/constants/auth.ts` | 加 `wechatLogin` 路径 + `IS_WEAPP` 编译期常量 |
| `src/utils/authStorage.ts` | `AuthUser` 加 `provider` 字段 |
| `src/services/tokenManager.ts` | `pickUser` 从 `user_metadata` 读 `provider`（登录响应里就有，不必等 `/auth/me`） |
| `src/services/auth.ts` | 加 `loginWithWechat()` + 微信错误码中文化 |
| `src/store/auth.tsx` | 暴露 `loginWithWechat`；`refreshUser` 回写 `provider` |
| `src/pages/login/index.tsx`（+ `.less`） | 小程序端「微信一键登录」为主入口，邮箱密码为次级入口，双向可切 |
| `src/pages/profile/security/index.tsx`（+ `.less`） | 微信用户隐藏改密 / 改邮箱 |

### 4.1 `src/constants/auth.ts`

```ts
export const AUTH_PATH = {
  // ... 现有 ...
  /** POST /auth/wechat/login：wx.login 的 code 换 token（仅微信小程序端可用） */
  wechatLogin: '/auth/wechat/login',
} as const

/**
 * 是否运行在微信小程序环境。
 *
 * 编译期常量：Taro 构建时把 `process.env.TARO_ENV` 替换成字面量，
 * 所以 H5 产物里 `IS_WEAPP` 恒为 false，微信分支会被静态消除，
 * 不会把 Taro.login 相关代码打进 H5 包。
 */
export const IS_WEAPP = process.env.TARO_ENV === 'weapp'
```

> 已从构建产物验证：H5 的 `dist/common.js` 里 `IS_WEAPP` 被折叠成 `!1`，
> 登录页 chunk 里**完全没有** wechat 引用；小程序的 `dist/pages/login/index.js`
> 里 entry 初值直接编译成 `"wechat"`。

### 4.2 `src/utils/authStorage.ts` + `tokenManager.ts`

`provider` 走两层：登录响应里的 JWT 已经带 `user_metadata.provider`，
先存进会话；`/auth/me` 之后再以服务端为准覆盖一次。

```ts
// utils/authStorage.ts —— AuthUser 增加
  /**
   * 登录来源：'wechat'=微信登录，其余为空（邮箱注册）。
   * 微信用户没有真邮箱（后端用占位邮箱 wx_xxx@wechat.local），
   * 前端据此隐藏「修改密码 / 修改邮箱」入口。
   */
  provider?: string | null
```

```ts
// services/tokenManager.ts —— pickUser 内
  const meta =
    (src.user_metadata as Record<string, any>) ??
    (claims?.user_metadata as Record<string, any>) ??
    {}

  // 登录来源（'wechat' = 微信登录）。后端建号时写进 user_metadata 并随 JWT 下发。
  const provider =
    (typeof meta.provider === 'string' && meta.provider) || undefined

  return { id: String(id), email, role, emailVerified, provider }
```

`user_metadata` 原来只从响应体的 `user` 上取，刷新接口常常不带 `user`，
补一条 JWT claims 兜底。

### 4.3 `src/services/auth.ts`

```ts
import Taro from '@tarojs/taro'
import { AUTH_PATH, IS_WEAPP } from '../constants/auth'

/**
 * 微信登录错误码 → 中文。
 *
 * 后端把微信返回的 errcode 原样编码成 `wechat_{errcode}`
 * （见 app/services/wechat.py），这里按 code 精确匹配，比匹配英文文案可靠。
 */
const WECHAT_ERROR_TEXT: Record<string, string> = {
  wechat_40029: '登录凭证已失效，请重新登录',
  wechat_45011: '操作过于频繁，请稍后再试',
  wechat_40226: '该微信账号已被限制登录',
  'wechat_-1': '微信服务暂时不可用，请稍后重试',
  wechat_login_failed: '微信登录失败，请确认小程序已配置真实 AppID',
  unsupported_env: '当前环境不支持微信登录',
}
```

在 `toFriendlyMessage` 的 `if (err instanceof ApiError)` 分支里，
**放在正则表之前**（正则匹配不到这类结构化 code）：

```ts
    // 微信错误码是结构化的，优先按 code 命中；其余 wechat_* 用服务端原文兜底
    const wxText = WECHAT_ERROR_TEXT[err.code]
    if (wxText) return wxText
    if (err.code.startsWith('wechat_')) return err.message || '微信登录失败，请重试'
```

登录函数：

```ts
/**
 * 微信小程序一键登录：wx.login() 拿 code → 后端换 token → 写入全局会话。
 *
 * 返回的 token 由 Supabase GoTrue 签发，与邮箱登录**完全同构**，
 * 所以续期、401 拦截、退出登录、RLS 全都不需要额外适配。
 *
 * 两个注意点：
 *   1. code 一次性且 5 分钟有效。失败必须重新 Taro.login() 换新 code，
 *      不能拿同一个 code 重试 —— 后端会直接报 40029。
 *   2. 不收昵称 / 头像：后端建号时用默认资料，用户可随后在资料页自行修改。
 *      登录时强弹授权会明显拖慢首次进入的转化。
 */
export async function loginWithWechat(): Promise<AuthSession> {
  if (!IS_WEAPP) {
    throw new ApiError('当前环境不支持微信登录', 400, 'unsupported_env')
  }

  let code = ''
  try {
    const res = await Taro.login()
    code = res?.code || ''
  } catch (err) {
    // 最常见原因：project.config.json 里还是 touristappid。
    // wx.login 抛的是英文原生错误，直接透给用户毫无意义，统一换成可操作的提示。
    console.warn('[auth] wx.login 调用失败:', err)
    throw new ApiError(
      '微信登录失败，请确认小程序已配置真实 AppID',
      500,
      'wechat_login_failed',
      err
    )
  }
  if (!code) {
    throw new ApiError('微信登录失败，未取到登录凭证', 500, 'wechat_login_failed')
  }

  const data = await request<TokenResponse>({
    url: AUTH_PATH.wechatLogin,
    data: { code },
    auth: false,
  })

  const session = toSession(data)
  if (!session) {
    throw new ApiError('登录失败：服务端未返回有效凭证', 500, 'invalid_token_response')
  }
  tokenManager.setSession(session)
  return session
}
```

> 后端 `WechatLoginRequest` 的 `nickname` / `avatar_url` 保留为可选字段，
> 前端暂不传 —— 留作后续「登录后补全微信资料」用。

### 4.4 `src/store/auth.tsx`

```ts
interface AuthContextValue {
  // ... 现有 ...
  /** 微信小程序一键登录。非小程序环境会抛 unsupported_env */
  loginWithWechat: () => Promise<AuthSession>
}
```

```tsx
const loginWithWechat = useCallback(async () => {
  const session = await authApi.loginWithWechat()
  // 登录后立刻拉一次资料：登录响应里只有 id/email/role，昵称、头像、
  // provider 都要等 /auth/me。登录页随后就 reLaunch 走了，不会触发
  // App 挂载时的那次 refreshUser，不主动拉就得到下次冷启动才有。
  // 失败不影响登录结果（会话已经写进去了），所以吞掉异常。
  void refreshUser().catch(() => {})
  return session
}, [refreshUser])
```

`refreshUser` 里补一行 `provider`（`/auth/me` 是权威来源，JWT 丢了也能补回来）：

```ts
provider: me.provider ?? prevUser?.provider ?? null,
```

### 4.5 `src/pages/login/index.tsx`

小程序端「微信一键登录」为主入口，邮箱密码折叠为次级入口，**双向可切**：

```tsx
type Mode = 'login' | 'register'
/** 小程序端有两种入口：微信一键登录 / 邮箱密码。H5 端恒为 password */
type Entry = 'wechat' | 'password'

const [entry, setEntry] = useState<Entry>(IS_WEAPP ? 'wechat' : 'password')
/** 小程序端且未切到「邮箱密码」时，只渲染微信一键登录 */
const showWechat = IS_WEAPP && entry === 'wechat'
const showPasswordForm = !showWechat
```

```tsx
const handleWechatLogin = useCallback(async () => {
  if (submitting) return
  setErrorMsg('')
  setNoticeMsg('')
  setSubmitting(true)
  try {
    await loginWithWechat()
    Taro.showToast({ title: '登录成功', icon: 'success' })
    setTimeout(goAfterAuth, 400)
  } catch (err) {
    setErrorMsg(toFriendlyMessage(err))
  } finally {
    setSubmitting(false)
  }
}, [submitting, loginWithWechat, goAfterAuth])
```

结构上是「二选一渲染」，H5 端 `showWechat` 恒为 false，只会渲染下面那一支：

```tsx
{showWechat && (
  <View className='login-form'>
    <View className={`wx-btn ${submitting ? 'is-loading' : ''}`}
          onClick={() => void handleWechatLogin()}>
      <Text className='wx-btn-text'>{submitting ? '登录中…' : '微信一键登录'}</Text>
    </View>
    {/* 错误提示条 */}
    <View className='login-switch'>
      <Text className='switch-link' onClick={() => switchEntry('password')}>
        使用邮箱密码登录
      </Text>
    </View>
  </View>
)}

{showPasswordForm && (
  <>
    {/* 登录/注册 tab + 原有邮箱密码表单，原样保留 */}
    {IS_WEAPP && (
      <View className='login-switch is-secondary'>
        <Text className='switch-link' onClick={() => switchEntry('wechat')}>
          使用微信一键登录
        </Text>
      </View>
    )}
  </>
)}
```

`.less` 只加两块：

```less
.wx-btn {
  height: 46px;
  border-radius: 10px;
  background: #07c160;              /* 微信品牌绿 */
  display: flex;
  align-items: center;
  justify-content: center;
  margin-top: 6px;
  box-shadow: 0 6px 16px rgba(7, 193, 96, 0.26);
  transition: opacity 0.2s ease;
}
.wx-btn:active { opacity: 0.85; }
.wx-btn.is-loading { opacity: 0.65; }
.wx-btn-text {
  font-size: 15px; font-weight: 600; color: #ffffff; letter-spacing: 1px;
}
/* 次级入口，与上方的注册/登录切换拉开距离 */
.login-switch.is-secondary { margin-top: 10px; }
```

### 4.6 `src/pages/profile/security/index.tsx`

微信用户整块隐藏改密 / 改邮箱 —— 他们下次登录走 code2session，不看密码，
改了也没用；占位邮箱更是收不到验证邮件。

```tsx
/**
 * 微信登录用户：后端用占位邮箱（wx_xxx@wechat.local）建号，
 * 改密码 / 改邮箱对它没有意义 —— 下次登录走的是 code2session，不看密码。
 * 所以整块隐藏，只保留登录方式说明。
 */
const isWechat = user?.provider === 'wechat'
```

顶部卡片同时换文案，避免把 `wx_xxx@wechat.local` 当成真邮箱展示给用户：

```tsx
<Text className='email-label'>
  {isWechat ? '当前登录方式' : '当前登录邮箱'}
</Text>
<View className='email-line'>
  <Text className='email-value'>
    {isWechat ? '微信一键登录' : user?.email || '未知'}
  </Text>
  {!isWechat && (
    <Text className={`email-badge${verified ? ' is-ok' : ''}`}>
      {verified ? '已验证' : '未验证'}
    </Text>
  )}
</View>
{isWechat && (
  <Text className='email-desc'>
    微信账号未绑定邮箱，密码与邮箱的修改入口不适用。
  </Text>
)}
```

两个卡片各自包一层 `{!isWechat && ( ... )}`。

---

## 5. 小程序端配置 checklist

这几步不做，代码全对也跑不通：

1. **`project.config.json` 的 `appid` 现在是 `touristappid`（游客）** —— 必须换成真实小程序 AppID，
   否则 `wx.login()` 拿到的 code 后端兑换不了。
2. **微信公众平台 → 开发 → 开发管理 → 服务器域名 → request 合法域名**，加上后端域名。
   必须是 **https + 443**，不支持 IP、不支持自定义端口。本地调试可在开发者工具里
   勾「不校验合法域名」。
3. 后端域名要在公网可达（Railway 域名可以；本地 `127.0.0.1:8000` 不行）。
4. `.env` 填 `WECHAT_APPID` / `WECHAT_APP_SECRET`（`WECHAT_LINK_SECRET` 留空即可，
   会回落到 `SUPABASE_JWT_SECRET`）。Railway 环境变量同步加一遍。

---

## 6. 联调前置项（实测状态）

> 2026-09-04 用真实基础设施逐条核实过，✅ 的都不用再做了。

| # | 项目 | 状态 | 证据 |
|---|---|---|---|
| 1 | 执行 `sql/wechat_auth.sql`（建 `user_identities` + `profiles.provider`） | ✅ 已完成 | PostgREST 查 `user_identities` 返回 200 `[]`；查 `profiles.provider` 返回 200。对照组 `profiles.total_stories` 正确报 42703「列不存在」，证明探测有效 |
| 2 | `.env` 填 `WECHAT_APPID` / `WECHAT_APP_SECRET` | ✅ 已填真实值 | 打假 code 返回的是 `wechat_40029`（code 无效），**不是** 503，说明凭证已被微信接受（appid/secret 错会返回 40013 / 40125） |
| 3 | `project.config.json` 的 `appid` 换成真实 AppID | ✅ 已完成 | 已从 `.env` 的 `WECHAT_APPID` 提取写入，值 `wxd733fdcc9cddfac6`（正则 `wx[0-9a-f]{16}` 校验 + JSON 回读比对） |
| 4 | 执行 `sql/wechat_bind.sql`（补 `user_identities.email_snapshot` + `profiles.wechat_bound`） | ✅ 已完成 | `scripts/_probe_wechat_bind.py` 探得两列均 200 存在，阴性对照正确报 42703 |
| 5 | 微信后台配置 request 合法域名 | ⚠️ 待确认 | 需 https + 443，不支持 IP / 自定义端口。见下 |

**第 5 项要填的域名**取决于前端 `src/constants/api.ts` 里 `API_ORIGIN` 指向哪：
生产就是那个域名；本地 `127.0.0.1:8000` **不行**（小程序不认 IP + 自定义端口），
真机调试只能靠开发者工具勾「不校验合法域名」。

### 已实测通过的核心链路（真实 Supabase，非 mock）

用一次性测试账号跑完 `admin_create_user → password grant → refresh` 后删除账号：

```
[1] admin_create_user   OK  user_id=04fbe701-...
[2] password grant      OK  access_token 894 字符
[3] JWT user_metadata   = {"email_verified": true, "openid": "...", "provider": "wechat"}
[4] refresh             OK  返回新 access_token
[cleanup] 删除测试账号 HTTP 200
```

**第 3 条最关键**：`provider` 确实随 JWT 下发，前端 `pickUser` 从
`user_metadata` 读 provider 的设计成立，登录响应里就能判定来源，不必等 `/auth/me`。

两个观察：

- **`refresh_token` 是 12 位不透明串**（如 `xbxx4dursgxf`），且**可重复使用**
  （同一 token 连续 refresh 两次都成功）—— 这个 Supabase 项目关掉了
  refresh token 轮换。前端 `tokenManager` 本就有「新 token 缺失则沿用旧的」兜底，
  兼容易；单飞逻辑在并发刷新时依然是必要的护栏。
- ~~`SUPABASE_ANON_KEY` 是无效占位符~~ ✅ 当前 `.env` 里是真实 anon JWT（`role: anon`）。
  另外 `SupabaseAuth._headers()` 已改成 **`anon` 缺失时回落 `service_role`**
  （GoTrue 两者都接受），以后 anon key 失效也不会让整条登录链路挂掉。

---

## 7. 验收步骤

```bash
# 1. 后端起来
curl http://127.0.0.1:8000/ready

# 2. 未配 appid 时应返回 503 service_unavailable（证明开关生效）
curl -X POST http://127.0.0.1:8000/api/v1/auth/wechat/login \
  -H 'Content-Type: application/json' -d '{"code":"any"}'

# 3. 配好 appid 后，假 code 会真的打到微信，应返回 422 code=wechat_40029
curl -X POST http://127.0.0.1:8000/api/v1/auth/wechat/login \
  -H 'Content-Type: application/json' -d '{"code":"invalid_code"}'

# 4. 小程序端真机/模拟器点「微信一键登录」
#    预期：Toast「登录成功」→ 跳首页

# 5. 用返回的 access_token 调一个业务接口，确认身份打通
curl http://127.0.0.1:8000/api/v1/auth/me -H "Authorization: Bearer <access_token>"
#    预期：provider = "wechat"，id 是合法 uuid

# 6. 等 access_token 过期前手动触发 /auth/refresh，确认续期可用
# 7. 数据库确认：user_identities 有 1 行，profiles 有 1 行，auth.users 有 1 行
```

前端：`npx tsc --noEmit` 0 报错 + `NODE_OPTIONS= npm run build:weapp` 成功。

---

## 8. 后续扩展

- **多端打通**：绑定微信开放平台后 `code2session` 会返回 `unionid`，
  `user_identities` 已留字段，届时按 `union_id` 查找即可让公众号/小程序/App 共用一个账号。
- **session_key 加密存储**：现在明文存库。当前没有任何功能用到它（不做手机号绑定），
  哪天要用它解密敏感数据时，建议改成 AES 加密存储。

---

## 9. 账号打通：微信 ⇄ 邮箱

> 本节是第 3、4 节的**后续改造**，已完成落地。

### 9.1 问题：一个 GoTrue 账号只有一个密码

初版微信登录用「占位邮箱 + HMAC(openid) 派生的确定性密码」走 password grant。
这在微信用户想绑定邮箱时会出现死结：

| 场景 | 后果 |
|---|---|
| 微信用户绑定邮箱后自设密码 | 密码被覆盖成用户设的 → 微信登录的 password grant 失败 |
| 走原有「重置回确定性密码」分支 | 微信登录恢复，但**用户刚设的邮箱密码失效** |
| 老邮箱用户绑定微信 | 不能重置成确定性密码，否则原密码失效 —— 无解 |

根因不是"绑定逻辑写得不对"，而是**两条登录路径抢同一个密码字段**。

### 9.2 解法：登录改用免密签发（magiclink）

GoTrue 的 admin 接口能在**完全不知道密码**的前提下给指定用户签发会话：

```python
# 1) 拿到用户当前邮箱（generate_link 只认 email，实测传 user_id 会 400）
user = await auth.admin_get_user(user_id)
# 2) 生成一次性登录链接（不真的发邮件）
link = await auth.generate_link(user["email"], "magiclink")
# 3) 服务端直接兑换 —— 必须是 POST + JSON body
session = await auth.verify_link(link["hashed_token"], "magiclink")
#    → access_token / refresh_token，与 sign_in 完全同构
```

三个实测确认的坑：

| 坑 | 表现 | 正确写法 |
|---|---|---|
| `generate_link` 不认 user_id | `400 An email address is required` | 传 email，先 `admin_get_user` 取 |
| `GET /verify?token_hash=` 无效 | `400 Verify requires a token or a token hash` | **POST + JSON body** |
| 老版本 GoTrue 无 `hashed_token` | 返回体里只有 `action_link` | 从 action_link 的 query 解析 `token` |

第 2 条尤其容易踩：网上流传的示例几乎都用 GET + query，在新版 GoTrue 上直接 400。

实测证据（一次性测试账号，用完即删）：

```
[1] 免密签发    sub 与老用户一致: True
[2] 老密码仍可登录: True   <<< 关键：微信登录没把邮箱密码顶掉
[3] 旧方案对照：admin 重置为确定性密码后，老密码可登录: False
[4] 绑定邮箱后重新签发: sub 仍是同一用户, token.email 已更新
```

`[2]` 与 `[3]` 的对比就是"死结被解开"的直接判据。

### 9.3 随之而来的清理

- `WeChatService.derive_password` 删除。密码改为建号时 `random_password()`
  随机生成后即丢弃 —— **刻意不可复现**，否则等于给账号留了绕过微信校验的后门。
- 删除 `/auth/wechat/login` 里「password grant 失败 → admin 重置密码 → 重试」
  分支。这条分支现在只会造成破坏。
- 测试 `test_login_never_touches_password`：FakeAuth 故意不实现 `sign_in`，
  端点若还依赖密码就会 AttributeError —— 把"不靠密码"变成可回归的契约。

### 9.4 新增端点

| 端点 | 作用 |
|---|---|
| `POST /auth/wechat/bind` | 已登录账号绑定微信（要求登录态，绝不建新号；openid 被占用 → 409） |
| `POST /auth/me/email/bind/start` | 发 6 位验证码到目标邮箱（邮箱已被占用 → 409） |
| `POST /auth/me/email/bind/confirm` | 校验验证码 → 改邮箱 → **换发新 token** |
| `POST /auth/me/password/set` | 微信用户设置登录密码（不校验当前密码） |

`confirm` 必须返回新 token：改 email 后旧 token 里的 email claim 仍是占位邮箱，
不换发的话前端要等下次刷新才看得到正确邮箱。

`password/set` 之所以不校验当前密码：微信用户建号时的密码是随机生成后即丢弃的，
本人无从得知，要求输入当前密码等于把这条路堵死。

### 9.5 安全边界

免密签发等价于「以任意用户身份登录」，因此：

- 只在 `code2session` 成功之后调用。openid 由微信侧签名保证，
  这是整条链路上唯一的强身份校验。
- `issue_session_for_user` 是 `SupabaseAuth` 的内部方法，**不暴露为 HTTP 端点**。
- 每次调用都记日志（含 openid 脱敏前缀）。

### 9.6 绑定记录指向已删除账号时的自愈

只要 `user_identities` 里有一条记录指向 `auth.users` 中不存在的账号
（后台手删、数据迁移丢数据、清理脚本误删），该微信用户就会**永久登录失败** ——
每次都命中这条记录，拿一个不存在的 user_id 去签发，且永远不会走到建号分支。

登录端点为此加了自愈：签发拿到 **404** 时，清掉这条绑定记录与可能残留的
profiles 行，然后重新建号。

```python
if user_id:
    try:
        data = await auth.issue_session_for_user(user_id)
    except AuthError as exc:
        if exc.status_code != 404:
            raise                      # 其它错误原样抛出，不能被掩盖
        await _forget_identity(db, user_id, openid)
        user_id = ""
```

只对 404 自愈。502 / 限流等错误若也走重建，会在上游抖动时重复建号。
测试 `test_non_404_signin_error_is_not_swallowed` 锁住这条边界。

### 9.7 `find_user_id_by_email` 的分页坑

绑定邮箱要靠它做「邮箱已被占用」校验。GoTrue 的 listUsers 不支持按 email
精确过滤，只能分页遍历 —— 而**它的返回体里没有 `total` 字段**（实测为 `None`）。

终止条件若写成依赖 total：

```python
total = int(payload.get("total") or 0)                    # → 0
if len(users) < per_page or page * per_page >= total:     # 满页时恒真 → break
```

用户数超过一页后，第一页满页就 break，后面的账号全部漏判 ——
占用校验失效，会把别人的登录邮箱顶掉。

正确做法只看「本页是否满」：`len(users) < per_page` 即已到最后一页。
`tests/test_supabase_find_user_by_email.py` 用 `per_page=2` 强制分页锁住这点，
并已通过变异测试验证（改回旧写法该测试确实会失败）。

### 9.8 已知限制

**Supabase 内置 SMTP 限流很紧**，实测约 60 秒 1 封，超限返回
`429 over_email_send_rate_limit`。绑定邮箱是低频操作，够用；
若用户反馈收不到码，在 Supabase 后台配自定义 SMTP 即可。

前端 `toFriendlyMessage` 已把该错误码映射成「发送过于频繁，请稍后再试」。

---

## 10. 打通后的端到端验证与三个真坑

`sql/wechat_bind.sql` 执行完后，用 `scripts/_e2e_wechat_bind_live.py` 跑了真实
Supabase + 真实本地 API 的全链路（只把 `code2session` 换成可控的假实现，因为它
需要真机 `wx.login` 才拿得到的 code）。13 项全通过：

```
[A] 邮箱老用户绑定微信，再用微信登录
  A1 绑定前 wechat_bound=false            PASS
  A2 绑定微信返回 200                      PASS
  A3 绑定后 wechat_bound=true             PASS
  A4 绑定后 user_id 未变                   PASS
  A5 email_snapshot 已落库                 PASS
  A6 微信登录落回同一 user_id              PASS   <<< 老用户数据不丢

[B] 微信用户设置密码，再用邮箱密码登录
  B1 微信首次登录建号成功                  PASS
  B2 /auth/me provider=wechat             PASS
  B3 占位邮箱形如 wx_xxx@wechat.local     PASS
  B4 设置密码返回 200                      PASS
  B5 邮箱 + 密码可登录（同一账号）         PASS   <<< 两种方式并存
  B6 设密码后微信登录仍落回同一 user_id    PASS

[C] 真实 dev server（8000 端口）冒烟
  C1 dev server 接受刚签发的 token        PASS
```

脚本自带清理，且**只删自己记录的 user_id / openid**——此前有过按邮箱后缀批量清理
误删真实账号的教训，绝不重复。

跑通过程中挖出三个真坑，都已修复并加了回归测试：

### 10.1 坑一：profiles 行可能不存在，绑定回写会静默丢失

**症状**：绑定微信返回 200，`/auth/me` 却永远 `wechat_bound=false` —— 前端一直显示
「绑定微信」，再点一次又撞 `wechat_already_bound`。

**根因**：邮箱注册（`/auth/register`）**不写 profiles**。profiles 只在三处创建：
微信首次登录的 `_seed_profile`、编辑资料、上传头像。从未动过资料的老用户根本没有
这一行，而回写用的是 `update`：

```python
await db.update("profiles", filters={"id": f"eq.{user.id}"}, data={"wechat_bound": True})
```

PostgREST 的 PATCH 匹配不到行时**返回空数组且不报错**，于是绑定看似成功、标记丢了。

**修复**：改用 `upsert`（`on_conflict="id"`）。`SupabaseClient.upsert` 固定带
`Prefer: resolution=merge-duplicates`，只覆盖传入的列，不会冲掉用户已填的昵称/头像。

**教训**：测试桩必须贴近真实。原 `FakeDB.update` 用 `setdefault` 凭空建行、
`FakeDB.upsert` 整行覆盖 —— 两种写法都让测试通过而线上照样出错。现已改成
「PATCH 不建行 + upsert 按 merge 合并」，并用变异测试验证（把修复回退成 `update`，
`test_bind_wechat_creates_profile_row_when_missing` 确实失败）。

### 10.2 坑二：本机时钟落后会让**所有**登录态 401

**症状**：`token 校验失败: The token is not yet valid (iat)`，每个带 token 的请求都 401。

**根因**：本机比 Supabase **慢 66 秒**，GoTrue 签发的 token 的 `iat` 落在本地时钟的
「未来」，PyJWT 的 `_validate_iat` 直接抛 `ImmatureSignatureError`。而 JWT 校验的
leeway 原本硬编码为 30 秒，扛不住 66 秒偏移。

**修复**：leeway 做成配置 `JWT_LEEWAY_SECONDS`，默认 **120 秒**；同时单独捕获
`ImmatureSignatureError`，给出可操作的提示（而不是让人对着 `iat` 发懵）：

```
登录凭证暂时不可用，可能是本机时间与服务器不同步，请校准系统时间后重试
```

leeway 同时作用于 `exp` / `nbf` / `iat`，放大到 120 秒是可接受的常规取舍；
若要收紧，在 `.env` 里覆盖即可。治本的做法仍是同步系统时间。

### 10.3 坑三：JWKS 偶发拉取超时，表现为「第一次 401，刷新就好了」

首次拉取 JWKS 正常约 1.5 秒，但实测遇到过一次撞满 10 秒超时，直接 401
`jwks_unavailable`；缓存命中后一切正常。

**修复**：`JWKSCache._refresh` 加一次重试（共 2 次机会，间隔 0.4 秒退避）。
失败时**不更新** `_fetched_at`，下次请求仍会重新拉取 —— 一次网络抖动不会被固化成
长期故障。`tests/test_jwks_cache.py` 覆盖「首次失败重试成功」「持续失败才报
jwks_unavailable」「失败不进缓存」「成功进缓存」四条。

> 补充：排查时还发现，本仓库里由助手启动的进程会被沙箱注入
> `HTTP_PROXY=127.0.0.1:624xx`，偶发不通时会放大这类超时。用户自己终端启动的进程
> 不走该代理。跑验证脚本时用
> `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy python ...` 排除干扰。

### 10.4 顺带清理

`WECHAT_LINK_SECRET` / `Settings._wechat_link_key` 是「确定性密码」时代的遗留，
随 9.3 的方案切换已成死代码，本次删除。`.env` 里那行留着无害（pydantic-settings
忽略未知字段），可自行清理。
