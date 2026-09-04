# 微信小程序登录改造方案

> 目标：在小程序端实现「微信一键登录」，且**现有业务代码零改动**。
> 后端 `docs/` 目录，前端 `C:\Applications\works\jbsttj-frontend`。

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

改动 4 个文件，核心是**新增一个登录方式**，现有邮箱登录完全保留（H5 端继续用）。

| 文件 | 改动 |
|---|---|
| `src/constants/auth.ts` | 加 2 条路径 |
| `src/services/auth.ts` | 加 `loginWithWechat()` |
| `src/store/auth.tsx` | 暴露 `loginWithWechat` |
| `src/pages/login/index.tsx` | 小程序端渲染「微信一键登录」为主入口 |

### 4.1 `src/constants/auth.ts`

```ts
export const AUTH_PATH = {
  // ... 现有 ...
  /** POST /auth/wechat/login：wx.login 的 code 换 token */
  wechatLogin: '/auth/wechat/login',
} as const

/** 是否运行在微信小程序环境。编译期常量，H5 侧会被 tree-shake 掉 */
export const IS_WEAPP = process.env.TARO_ENV === 'weapp'
```

### 4.2 `src/services/auth.ts`

```ts
import Taro from '@tarojs/taro'
import { AUTH_PATH, IS_WEAPP } from '../constants/auth'

/** 微信小程序一键登录：wx.login() 拿 code → 后端换 token */
export async function loginWithWechat(profile?: {
  nickname?: string
  avatarUrl?: string
}): Promise<AuthSession> {
  if (!IS_WEAPP) {
    throw new ApiError('当前环境不支持微信登录', 400, 'unsupported_env')
  }

  // code 一次性且 5 分钟有效：失败必须重新 login，不能拿旧 code 重试
  const { code } = await Taro.login()

  const data = await request<TokenResponse>({
    url: AUTH_PATH.wechatLogin,
    data: {
      code,
      ...(profile?.nickname ? { nickname: profile.nickname } : {}),
      ...(profile?.avatarUrl ? { avatar_url: profile.avatarUrl } : {}),
    },
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

### 4.3 `src/store/auth.tsx`

在 `AuthContextValue` 里加一行，并在 `value` 里补上：

```ts
interface AuthContextValue {
  // ... 现有 ...
  loginWithWechat: (profile?: { nickname?: string; avatarUrl?: string }) => Promise<AuthSession>
}
```

```tsx
const loginWithWechat = useCallback(async (profile?: { nickname?: string; avatarUrl?: string }) => {
  return authApi.loginWithWechat(profile)
}, [])
```

### 4.4 `src/pages/login/index.tsx`

小程序端把「微信一键登录」作为主按钮，邮箱密码折叠到「更多登录方式」：

```tsx
import { AUTH_PATH, HOME_PAGE, IS_WEAPP, PASSWORD_MIN_LENGTH } from '../../constants/auth'

const [usePassword, setUsePassword] = useState(!IS_WEAPP)

const handleWechatLogin = useCallback(async () => {
  if (submitting) return
  setErrorMsg('')
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

在 `.login-form` 顶部插入（小程序端）：

```tsx
{IS_WEAPP && !usePassword && (
  <>
    <View className={`submit-btn is-primary ${submitting ? 'is-loading' : ''}`} onClick={() => void handleWechatLogin()}>
      <Text className='submit-text'>{submitting ? '登录中…' : '微信一键登录'}</Text>
    </View>
    <View className='login-switch' onClick={() => setUsePassword(true)}>
      <Text className='switch-link'>使用邮箱密码登录</Text>
    </View>
  </>
)}

{(!IS_WEAPP || usePassword) && (
  <>
    {/* 现有邮箱/密码表单，原样保留 */}
  </>
)}
```

微信头像/昵称（可选）：小程序 2022 年后 `getUserProfile` 已收回，改用
`<button open-type="chooseAvatar">` + `<input type="nickname">`，
在**资料编辑页**补齐即可，登录时不强制 —— 首次登录用默认头像，体验更顺。

### 4.5 资料页小改

`src/pages/profile/security/index` 里的「修改密码 / 修改邮箱」入口，
对 `provider === 'wechat'` 的用户隐藏（占位邮箱改了也没意义）：

```tsx
{user?.provider !== 'wechat' && <ChangePasswordEntry />}
```

`AuthUser` 需要先补一个 `provider?: string` 字段（在 `store/auth.tsx` 的
`refreshUser` 里从 `me.provider` 读出来写回会话）。

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

## 6. 前置阻塞项

1. **`sql/wechat_auth.sql` 需要你在 Supabase SQL Editor 里执行**（建 `user_identities`
   表 + 给 `profiles` 补 `provider` 列）。不执行的话登录会在建绑定表那步 502。

2. ~~`SUPABASE_ANON_KEY` 是无效占位符~~ ✅ 已确认当前 `.env` 里是真实的 anon JWT
   （`role: anon`），password grant 可用。
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
- **账号合并**：已有邮箱账号的用户想绑微信 —— 加一个
  `POST /auth/wechat/bind`（需登录态），把 openid 绑到当前 `user.id`。
  反向（微信用户绑邮箱）同理。
- **session_key 加密存储**：现在明文存库。当前没有任何功能用到它（不做手机号绑定），
  哪天要用它解密敏感数据时，建议改成 AES 加密存储。
