"""微信小程序登录链路单元测试。

微信的 code2session 与 Supabase GoTrue 都没法在 CI 里真机调通，这里用假对象
替掉三个依赖（DB / Auth / WeChat），覆盖登录主链路与两条异常恢复分支：

  1. 首次登录：建 GoTrue 账号 → 写 user_identities → 播种 profiles → 拿 token
  2. 再次登录：命中绑定表，直接 password grant，不再建号
  3. 账号已存在：admin_create_user 报「已注册」→ 用确定性密码登录反查 user_id
  4. 密码被改：password grant 报 400 → admin 重置回确定性密码后重试成功

依赖替换走 FastAPI 的 dependency_overrides，不需要真实网络与数据库。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import auth as auth_api
from app.core.exceptions import AuthError
from app.main import app
from app.services.wechat import WeChatService

OPENID = "oTEST_OPENID_0001"
UNIONID = "uTEST_UNION_0001"
USER_ID = "11111111-2222-3333-4444-555555555555"
USER_ID_2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class FakeDB:
    """只实现微信登录用得到的三个方法，按主键存内存字典。"""

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
            key = (self._eq(filters.get("provider", "")), self._eq(filters.get("provider_uid", "")))
            return self.identities.get(key)
        if table == "profiles":
            return self.profiles.get(self._eq(filters.get("id", "")))
        return None

    async def upsert(self, table, data, on_conflict):
        self.upserts.append((table, dict(data), on_conflict))
        if table == "user_identities":
            self.identities[(data["provider"], data["provider_uid"])] = dict(data)
        else:
            self.profiles[data["id"]] = dict(data)
        return [data]

    async def update(self, table, *, filters, data):
        self.updates.append((table, dict(filters), dict(data)))
        if table == "profiles":
            row = self.profiles.setdefault(self._eq(filters["id"]), {})
            row.update(data)
            return [row]
        return []


class FakeAuth:
    def __init__(self, *, create_error: Exception | None = None, signin_fail_first: bool = False):
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.signin_calls = 0
        self._create_error = create_error
        self._signin_fail_first = signin_fail_first

    async def admin_create_user(self, attrs):
        if self._create_error is not None:
            raise self._create_error
        self.created.append(attrs)
        return {"id": USER_ID}

    async def sign_in(self, email, password):
        self.signin_calls += 1
        if self._signin_fail_first and self.signin_calls == 1:
            raise AuthError("Invalid login credentials", status_code=400)
        return {
            "access_token": f"at-{self.signin_calls}",
            "refresh_token": "rt-1",
            "expires_in": 3600,
            "user": {"id": USER_ID, "email": email},
        }

    async def admin_update_user(self, user_id, attrs):
        self.updated.append({"user_id": user_id, **attrs})
        return {}


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


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _post(client, **extra):
    return client.post("/api/v1/auth/wechat/login", json={"code": "wx-code-123", **extra})


def test_first_login_creates_account_and_profile(client):
    db, auth = FakeDB(), FakeAuth()
    _override(db, auth)

    resp = _post(client, nickname="阿强", avatar_url="https://wx.qlogo.cn/a.png")

    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "at-1"
    assert body["refresh_token"] == "rt-1"

    # 建号：占位邮箱 + 确定性密码 + 强制邮箱已验证
    created = auth.created[0]
    assert created["email"] == WeChatService.placeholder_email(OPENID)
    assert created["email_confirm"] is True
    assert created["password"].startswith("Jbs!wx")
    assert created["user_metadata"]["provider"] == "wechat"

    # 绑定表：session_key 入库，raw 里不含 session_key
    identity = db.identities[("wechat", OPENID)]
    assert identity["user_id"] == USER_ID
    assert identity["union_id"] == UNIONID
    assert identity["session_key"] == "sk-test"
    assert "session_key" not in identity["raw"]

    # profiles 播种了昵称与头像
    profile = db.profiles[USER_ID]
    assert profile["nickname"] == "阿强"
    assert profile["avatar_url"] == "https://wx.qlogo.cn/a.png"
    assert profile["provider"] == "wechat"


def test_second_login_reuses_binding_without_recreating(client):
    db, auth = FakeDB(), FakeAuth()
    _override(db, auth)
    _post(client, nickname="阿强")

    # 换个 auth 实例模拟新会话，但绑定表仍在
    auth2 = FakeAuth()
    app.dependency_overrides[auth_api.get_supabase_auth] = lambda: auth2
    resp = _post(client, nickname="改过的昵称")

    assert resp.status_code == 200
    assert auth2.created == [], "已绑定的用户不应重复建号"
    # 已存在资料时不得覆盖用户自己改过的昵称
    assert db.profiles[USER_ID]["nickname"] == "阿强"
    # 回写 last_login_at
    assert any(t == "user_identities" for t, _, _ in db.updates)


def test_existing_gotrue_account_is_recovered_by_signin(client):
    db, auth = FakeDB(), FakeAuth(
        create_error=AuthError("User already registered", status_code=422)
    )
    _override(db, auth)

    resp = _post(client)

    assert resp.status_code == 200
    assert auth.created == [], "建号失败不应留下空壳"
    assert db.identities[("wechat", OPENID)]["user_id"] == USER_ID


def test_password_changed_triggers_admin_reset_then_retry(client):
    db, auth = FakeDB(), FakeAuth(signin_fail_first=True)
    _override(db, auth)
    db.identities[("wechat", OPENID)] = {"user_id": USER_ID, "provider": "wechat", "provider_uid": OPENID}

    resp = _post(client)

    assert resp.status_code == 200
    assert resp.json()["access_token"] == "at-2", "重置密码后应重试并成功"
    assert auth.signin_calls == 2
    assert auth.updated[0]["user_id"] == USER_ID
    assert auth.updated[0]["password"].startswith("Jbs!wx")


def test_derived_password_is_stable_and_unique():
    wx = FakeWX()
    assert wx.derive_password(OPENID) == wx.derive_password(OPENID)
    assert wx.derive_password(OPENID) != wx.derive_password("oOTHER")
    assert len(wx.derive_password(OPENID)) == 30
    # 占位邮箱不能泄露 openid
    email = WeChatService.placeholder_email(OPENID)
    assert OPENID not in email
    assert email.endswith("@wechat.local")
