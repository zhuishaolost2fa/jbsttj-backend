"""微信小程序登录与账号打通的单元测试。

微信 code2session、GoTrue 都没法在 CI 里真机调通，这里用假对象替掉三个依赖
（DB / Auth / WeChat），覆盖登录主链路、绑定两个方向与关键不变量：

  1. 首次登录：建 GoTrue 账号 → 写 user_identities → 播种 profiles → 免密签发
  2. 再次登录：命中绑定表直接签发，不再建号，且不覆盖用户改过的昵称
  3. 账号已存在：admin_create_user 报「已注册」→ 按邮箱反查 user_id 补救
  4. 占位邮箱不泄露 openid
  5. 微信绑定：已登录账号挂上 openid；openid 被占用返回 409
  6. 绑定邮箱：校验验证码后改邮箱并重新签发 token

最关键的契约是 **登录不再依赖密码**：GoTrue 一个账号只有一个密码，若沿用
password grant，微信登录会把用户自设的邮箱密码顶掉。测试 2 的
「不得出现任何 sign_in / 改密码调用」就是对这条契约的断言。

依赖替换走 FastAPI 的 dependency_overrides，不需要真实网络与数据库。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import auth as auth_api
from app.core.exceptions import AuthError, ConflictError
from app.core.security import CurrentUser
from app.main import app
from app.services.wechat import WeChatService

OPENID = "oTEST_OPENID_0001"
UNIONID = "uTEST_UNION_0001"
USER_ID = "11111111-2222-3333-4444-555555555555"
USER_ID_2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PLACEHOLDER_EMAIL = WeChatService.placeholder_email(OPENID)
REAL_EMAIL = "someone@example.com"


class FakeDB:
    """只实现登录与绑定用得到的三个方法，按主键存内存字典。"""

    def __init__(self) -> None:
        self.available = True
        self.identities: dict[tuple[str, str], dict] = {}
        self.profiles: dict[str, dict] = {}
        self.upserts: list = []
        self.updates: list = []

    @staticmethod
    def _eq(value: str) -> str:
        return value[3:] if value.startswith("eq.") else value

    async def select_one(self, table, *, filters, columns="*"):
        if table == "user_identities":
            prov = self._eq(filters.get("provider", ""))
            uid = self._eq(filters.get("provider_uid", ""))
            if uid:
                return self.identities.get((prov, uid))
            # 按 user_id + provider 查（绑定邮箱时回写快照用）
            owner = self._eq(filters.get("user_id", ""))
            for row in self.identities.values():
                if row.get("user_id") == owner and row.get("provider") == prov:
                    return row
            return None
        if table == "profiles":
            return self.profiles.get(self._eq(filters.get("id", "")))
        return None

    async def upsert(self, table, data, on_conflict):
        """按 PostgREST 的 merge-duplicates 语义实现：只覆盖本次传入的列。

        SupabaseClient.upsert 固定带 `Prefer: resolution=merge-duplicates`，
        整行覆盖的桩会放过「绑定时冲掉用户昵称」这类 bug。
        """
        self.upserts.append((table, dict(data), on_conflict))
        if table == "user_identities":
            self.identities[(data["provider"], data["provider_uid"])] = dict(data)
            return [dict(data)]
        row = self.profiles.setdefault(data["id"], {})
        row.update(data)
        return [dict(row)]

    async def delete(self, table, *, filters):
        self.updates.append((table, dict(filters), {"__deleted__": True}))
        if table == "user_identities":
            key = (self._eq(filters.get("provider", "")), self._eq(filters.get("provider_uid", "")))
            self.identities.pop(key, None)
        elif table == "profiles":
            self.profiles.pop(self._eq(filters.get("id", "")), None)
        return []

    async def update(self, table, *, filters, data):
        """刻意**不**凭空建行。

        真实 PostgREST 的 PATCH 只更新已存在的行，匹配不到就返回空数组且不报错
        —— 这个差异正是「绑定微信后 wechat_bound 丢失」的成因：profiles 行不存在
        时 update 静默失效。若这里用 setdefault 建行，测试会通过但线上照样丢数据。
        """
        self.updates.append((table, dict(filters), dict(data)))
        if table == "profiles":
            row = self.profiles.get(self._eq(filters["id"]))
            if row is None:
                return []
            row.update(data)
            return [row]
        if table == "user_identities":
            matched = []
            for row in self.identities.values():
                if row.get("provider") == self._eq(filters.get("provider", "")):
                    owner = self._eq(filters.get("user_id", ""))
                    if owner and row.get("user_id") != owner:
                        continue
                    row.update(data)
                    matched.append(row)
            return matched
        return []


class FakeAuth:
    """记录所有调用；sign_in / password 相关调用被显式记录，用于断言「不靠密码」。

    注意 issue_session_for_user 是真实 SupabaseAuth 的方法，FakeAuth 只需提供
    同名方法即可 —— 登录端点不再关心密码，因此 FakeAuth 里**没有** sign_in。
    """

    def __init__(
        self,
        *,
        create_error: Exception | None = None,
        lookup: str | None = None,
        issue_404_for: set[str] | None = None,
    ):
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.issued: list[str] = []
        self.otp_sent: list[str] = []
        self._create_error = create_error
        self._lookup = lookup or ""  # find_user_id_by_email 的返回值
        # 这些 user_id 签发时假装「账号在 auth.users 里已不存在」
        self._issue_404_for = issue_404_for or set()
        self.email = PLACEHOLDER_EMAIL

    async def issue_session_for_user(self, user_id):
        if user_id in self._issue_404_for:
            # 模拟 GoTrue 账号已被删除：真实 SupabaseAuth 会在 admin_get_user
            # 拿到 404 后原样抛出 AuthError，登录端点据此判断需要自愈
            raise AuthError("User not found", status_code=404)
        self.issued.append(user_id)
        return {
            "access_token": f"at-{len(self.issued)}",
            "refresh_token": "rt-1",
            "expires_in": 3600,
            "user": {"id": user_id, "email": self.email},
        }

    async def admin_create_user(self, attrs):
        if self._create_error is not None:
            raise self._create_error
        self.created.append(attrs)
        return {"id": USER_ID}

    async def admin_update_user(self, user_id, attrs):
        self.updated.append({"user_id": user_id, **attrs})
        if "email" in attrs:
            self.email = attrs["email"]
        return {}

    async def admin_get_user(self, user_id):
        return {"id": user_id, "email": self.email}

    async def send_email_otp(self, email):
        self.otp_sent.append(email)

    async def verify_email_otp(self, email, token):
        if token != "123456":
            raise AuthError("invalid", status_code=400)
        return {"access_token": "otp-at"}

    async def find_user_id_by_email(self, email):
        return self._lookup


class FakeWX(WeChatService):
    @property
    def enabled(self) -> bool:
        return True

    async def code2session(self, code: str) -> dict:
        return {"openid": OPENID, "unionid": UNIONID, "session_key": "sk-test"}


@pytest.fixture
def client():
    return TestClient(app)


def _override(db: FakeDB, auth: FakeAuth):
    app.dependency_overrides[auth_api.get_supabase] = lambda: db
    app.dependency_overrides[auth_api.get_supabase_auth] = lambda: auth
    app.dependency_overrides[auth_api.get_wechat_service] = lambda: FakeWX()


def _as_user():
    app.dependency_overrides[auth_api.get_current_user] = lambda: CurrentUser(
        id=USER_ID, email=PLACEHOLDER_EMAIL, role="authenticated", is_service=False, claims={}
    )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _post(client, **extra):
    return client.post("/api/v1/auth/wechat/login", json={"code": "wx-code-123", **extra})


# ---------------- 登录主链路 ----------------

def test_first_login_creates_account_and_profile(client):
    db, auth = FakeDB(), FakeAuth()
    _override(db, auth)

    resp = _post(client, nickname="阿强", avatar_url="https://wx.qlogo.cn/a.png")

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "at-1"

    # 建号：占位邮箱 + 随机密码（不再是确定性密码）+ 强制邮箱已验证
    created = auth.created[0]
    assert created["email"] == PLACEHOLDER_EMAIL
    assert created["email_confirm"] is True
    assert created["user_metadata"]["provider"] == "wechat"

    # 绑定表：记录邮箱快照；session_key 入库但 raw 里不含它
    identity = db.identities[("wechat", OPENID)]
    assert identity["user_id"] == USER_ID
    assert identity["union_id"] == UNIONID
    assert identity["email_snapshot"] == PLACEHOLDER_EMAIL
    assert identity["session_key"] == "sk-test"
    assert "session_key" not in identity["raw"]

    # profiles 播种了昵称与头像
    profile = db.profiles[USER_ID]
    assert profile["nickname"] == "阿强"
    assert profile["avatar_url"] == "https://wx.qlogo.cn/a.png"
    assert profile["provider"] == "wechat"


def test_login_never_touches_password(client):
    """核心契约：登录链路不得出现任何密码操作。

    GoTrue 一个账号只有一个密码，password grant 会把用户自设密码顶掉。
    FakeAuth 故意不实现 sign_in —— 只要端点还依赖密码，这里就会 AttributeError。
    """
    db, auth = FakeDB(), FakeAuth()
    _override(db, auth)
    db.identities[("wechat", OPENID)] = {
        "user_id": USER_ID, "provider": "wechat", "provider_uid": OPENID
    }

    resp = _post(client)

    assert resp.status_code == 200
    assert auth.updated == [], "登录不得修改任何账号属性（尤其不能改密码）"
    assert auth.issued == [USER_ID]


def test_second_login_reuses_binding_without_recreating(client):
    db, auth = FakeDB(), FakeAuth()
    _override(db, auth)
    _post(client, nickname="阿强")

    auth2 = FakeAuth()
    app.dependency_overrides[auth_api.get_supabase_auth] = lambda: auth2
    resp = _post(client, nickname="改过的昵称")

    assert resp.status_code == 200
    assert auth2.created == [], "已绑定的用户不应重复建号"
    # 已存在资料时不得覆盖用户自己改过的昵称
    assert db.profiles[USER_ID]["nickname"] == "阿强"
    assert any(t == "user_identities" for t, _, _ in db.updates)


def test_existing_gotrue_account_is_recovered_by_email_lookup(client):
    """建号报「已注册」时按邮箱反查，不能让网络抖动把用户永久锁死。"""
    db, auth = FakeDB(), FakeAuth(
        create_error=AuthError("User already registered", status_code=422),
        lookup=USER_ID,
    )
    _override(db, auth)

    resp = _post(client)

    assert resp.status_code == 200
    assert auth.created == [], "建号失败不应留下空壳"
    assert db.identities[("wechat", OPENID)]["user_id"] == USER_ID


def test_stale_binding_to_deleted_account_is_self_healed(client):
    """绑定记录指向已删除的账号时必须自愈，否则该微信用户永久登录失败。

    真实场景：有人在 Supabase 后台手删了 auth.users 里的账号、数据迁移丢数据、
    或清理脚本误删。只要绑定表还留着那条记录，每次登录都会命中它，
    拿一个不存在的 user_id 去签发 —— 用户就此锁死。
    """
    db, auth = FakeDB(), FakeAuth(issue_404_for={USER_ID_2})
    _override(db, auth)
    db.identities[("wechat", OPENID)] = {
        "user_id": USER_ID_2, "provider": "wechat", "provider_uid": OPENID
    }

    resp = _post(client)

    assert resp.status_code == 200
    # 旧绑定必须被清掉，否则下次登录还是走同一条死路
    assert ("wechat", OPENID) not in db.identities or (
        db.identities[("wechat", OPENID)]["user_id"] != USER_ID_2
    )
    # 并重新建号、重新绑定
    assert auth.created, "应重新建号"
    assert db.identities[("wechat", OPENID)]["user_id"] == USER_ID


def test_non_404_signin_error_is_not_swallowed(client):
    """只有 404（账号不存在）才自愈；其它错误必须原样抛出，不能掩盖真故障。"""
    db, auth = FakeDB(), FakeAuth()
    _override(db, auth)
    db.identities[("wechat", OPENID)] = {
        "user_id": USER_ID, "provider": "wechat", "provider_uid": OPENID
    }

    async def boom(_uid):
        raise AuthError("上游认证服务故障", status_code=502)

    auth.issue_session_for_user = boom

    resp = _post(client)

    assert resp.status_code >= 500
    assert auth.created == [], "502 不应触发重建，否则会重复建号"


def test_placeholder_email_hides_openid():
    email = WeChatService.placeholder_email(OPENID)
    assert OPENID not in email
    assert email.endswith("@wechat.local")
    assert email != WeChatService.placeholder_email("oOTHER")


def test_random_password_is_not_reproducible():
    """随机密码必须每次不同 —— 可复现就等于给账号留了绕过微信校验的后门。"""
    a, b = WeChatService.random_password(), WeChatService.random_password()
    assert a != b
    assert len(a) >= 20


# ---------------- 绑定：邮箱账号 → 微信 ----------------

def test_bind_wechat_to_current_account(client):
    db, auth = FakeDB(), FakeAuth()
    _override(db, auth)
    _as_user()

    resp = client.post("/api/v1/auth/wechat/bind", json={"code": "wx-code-123"})

    assert resp.status_code == 200
    identity = db.identities[("wechat", OPENID)]
    assert identity["user_id"] == USER_ID, "必须绑到当前登录账号，而不是新建账号"
    assert auth.created == [], "绑定绝不能创建新账号"
    assert db.profiles[USER_ID]["wechat_bound"] is True


def test_bind_wechat_creates_profile_row_when_missing(client):
    """profile 行不存在时，绑定必须照样把 wechat_bound 落下。

    邮箱注册（/auth/register）不写 profiles —— 只有微信首次登录、编辑资料、
    传头像才会建行。这类老用户绑微信时若用 update，PATCH 匹配 0 行且不报错，
    结果是：绑定成功、/auth/me 却永远返回 wechat_bound=false，前端一直显示
    「绑定微信」，再点一次又撞 wechat_already_bound。
    """
    db, auth = FakeDB(), FakeAuth()
    _override(db, auth)
    _as_user()
    assert USER_ID not in db.profiles, "前置条件：该用户没有 profile 行"

    resp = client.post("/api/v1/auth/wechat/bind", json={"code": "wx-code-123"})

    assert resp.status_code == 200
    assert db.profiles[USER_ID]["wechat_bound"] is True


def test_bind_wechat_preserves_existing_profile_fields(client):
    """绑定只改 wechat_bound，不能把用户已填的昵称冲掉（merge-duplicates 语义）。"""
    db, auth = FakeDB(), FakeAuth()
    _override(db, auth)
    _as_user()
    db.profiles[USER_ID] = {"id": USER_ID, "nickname": "老玩家", "avatar_url": "https://x/a.png"}

    resp = client.post("/api/v1/auth/wechat/bind", json={"code": "wx-code-123"})

    assert resp.status_code == 200
    assert db.profiles[USER_ID]["nickname"] == "老玩家"
    assert db.profiles[USER_ID]["avatar_url"] == "https://x/a.png"
    assert db.profiles[USER_ID]["wechat_bound"] is True


def test_bind_wechat_rejects_openid_owned_by_another_user(client):
    db, auth = FakeDB(), FakeAuth()
    _override(db, auth)
    _as_user()
    db.identities[("wechat", OPENID)] = {
        "user_id": USER_ID_2, "provider": "wechat", "provider_uid": OPENID
    }

    resp = client.post("/api/v1/auth/wechat/bind", json={"code": "wx-code-123"})

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "wechat_already_bound"


def test_bind_wechat_is_idempotent_for_same_user(client):
    db, auth = FakeDB(), FakeAuth()
    _override(db, auth)
    _as_user()
    db.identities[("wechat", OPENID)] = {
        "user_id": USER_ID, "provider": "wechat", "provider_uid": OPENID
    }

    resp = client.post("/api/v1/auth/wechat/bind", json={"code": "wx-code-123"})

    assert resp.status_code == 200
    assert "已绑定" in resp.json()["message"]


# ---------------- 绑定：微信账号 → 邮箱 ----------------

def test_bind_email_confirm_updates_email_and_reissues_token(client):
    db, auth = FakeDB(), FakeAuth()
    _override(db, auth)
    _as_user()
    start = client.post("/api/v1/auth/me/email/bind/start", json={"email": REAL_EMAIL})
    assert start.status_code == 200
    assert auth.otp_sent == [REAL_EMAIL]

    resp = client.post(
        "/api/v1/auth/me/email/bind/confirm", json={"email": REAL_EMAIL, "code": "123456"}
    )

    assert resp.status_code == 200
    # 邮箱已改成真实邮箱，且重新签发的 token 里也是新邮箱
    assert {"user_id": USER_ID, "email": REAL_EMAIL, "email_confirm": True} in auth.updated
    assert resp.json()["user"]["email"] == REAL_EMAIL
    # 绑定表快照同步，避免下次登录还拿旧邮箱去 generate_link
    assert any(
        t == "user_identities" and d.get("email_snapshot") == REAL_EMAIL
        for t, _, d in db.updates
    )


def test_bind_email_rejects_wrong_code(client):
    db, auth = FakeDB(), FakeAuth()
    _override(db, auth)
    _as_user()
    resp = client.post(
        "/api/v1/auth/me/email/bind/confirm", json={"email": REAL_EMAIL, "code": "000000"}
    )

    assert resp.status_code == 422
    assert auth.updated == [], "验证码错误时绝不能改邮箱"


def test_bind_email_rejects_email_taken_by_another_user(client):
    db, auth = FakeDB(), FakeAuth(lookup=USER_ID_2)
    _override(db, auth)
    _as_user()

    resp = client.post("/api/v1/auth/me/email/bind/start", json={"email": REAL_EMAIL})

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "email_taken"
    assert auth.otp_sent == [], "邮箱已被占用时不应发信"
