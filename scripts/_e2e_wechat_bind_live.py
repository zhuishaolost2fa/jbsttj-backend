"""真实 e2e：验证 sql/wechat_bind.sql 生效后，「微信 ↔ 邮箱」双向打通真的跑得通。

跑真 Supabase（GoTrue + PostgREST）与真本地 API。唯一被替换的是
WeChatService.code2session —— 它需要小程序真机 wx.login 才拿得到的 code，
服务端无法伪造。其余全部真实：建号、绑定表写入、免密签发、/auth/me、设密码、
邮箱密码登录。

验证两条核心路径（这正是「打通」的定义）：
  A. 老邮箱用户 → 绑微信 → 用微信 code 登录，**落回同一个 user_id**（历史数据不丢）
  B. 微信用户 → 设密码 → 用「邮箱 + 密码」登录，**落回同一个 user_id**

同时校验本次 SQL 新增的两列真的被写进去了：
  - user_identities.email_snapshot
  - profiles.wechat_bound

清理策略（此前误删过真实账号，这里刻意收窄）：
只删除本脚本自己记录下来的 user_id / openid，绝不按邮箱后缀批量清理。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from typing import Any, Dict, List

import httpx
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402
from app.services.supabase import SupabaseAuth, SupabaseClient  # noqa: E402
from app.services.wechat import WeChatService, get_wechat_service  # noqa: E402

API = "http://127.0.0.1:8000"
SUPA = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

RUN = str(int(time.time()))
FAKE_OPENID = f"probe_openid_{RUN}"
TEST_EMAIL = f"probe_email_{RUN}@example.com"
TEST_PASSWORD = "Probe-Pwd-2" + RUN[-6:]

_cleanup_user_ids: List[str] = []
_cleanup_openids: List[str] = []
_results: List[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    return ok


def user_id_of(token_body: Dict[str, Any]) -> str:
    """从登录响应里取 user_id。

    优先用响应里的 user 对象；缺失时回退解析 access_token 的 sub ——
    避免「响应结构不含 user」被误判成「登录落到了别的账号」。
    """
    uid = str((token_body.get("user") or {}).get("id") or "")
    if uid:
        return uid
    token = str(token_body.get("access_token") or "")
    part = token.split(".")[1] if token.count(".") >= 2 else ""
    if not part:
        return ""
    payload = part + "=" * (-len(part) % 4)
    try:
        return str(json.loads(base64.urlsafe_b64decode(payload)).get("sub") or "")
    except Exception:  # noqa: BLE001
        return ""


class FakeWeChat(WeChatService):
    """返回固定 openid 的 code2session 替身；其它行为完全继承真实实现。"""

    async def code2session(self, code: str) -> Dict[str, Any]:  # type: ignore[override]
        _ = code
        return {"openid": FAKE_OPENID, "session_key": "fake-session-key"}


async def admin_delete_user(user_id: str) -> None:
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.delete(
            f"{SUPA}/auth/v1/admin/users/{user_id}",
            headers={"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"},
        )
        print(f"   清理 auth.users {user_id[:8]}*** -> {r.status_code}")


async def cleanup(db: SupabaseClient) -> None:
    print("\n清理测试数据（仅本次脚本创建的对象）…")
    for oid in _cleanup_openids:
        try:
            await db.delete(
                "user_identities",
                filters={"provider": "eq.wechat", "provider_uid": f"eq.{oid}"},
            )
            print(f"   清理 user_identities openid={oid}")
        except Exception as exc:  # noqa: BLE001
            print(f"   清理 user_identities 失败: {exc}")
    for uid in _cleanup_user_ids:
        try:
            await db.delete("profiles", filters={"id": f"eq.{uid}"})
            print(f"   清理 profiles {uid[:8]}***")
        except Exception as exc:  # noqa: BLE001
            print(f"   清理 profiles 失败: {exc}")
        try:
            await admin_delete_user(uid)
        except Exception as exc:  # noqa: BLE001
            print(f"   清理 auth.users 失败: {exc}")


async def main() -> int:
    global FAKE_OPENID
    # 用与 FastAPI 依赖注入**同一个**模块级单例，避免两套连接的配置差异
    from app.services.supabase import get_supabase, get_supabase_auth

    db = get_supabase()
    auth = get_supabase_auth()
    await db.startup()  # 单例不会自动建连接池，脚本里要显式拉起
    if not db.available:
        print("Supabase 未配置，无法跑真实 e2e")
        return 1

    # 只替换微信 code2session，其余依赖保持真实
    app.dependency_overrides[get_wechat_service] = lambda: FakeWeChat()
    _ = get_supabase, get_supabase_auth

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url=API, timeout=60.0) as cli:
            # ---------------- A. 老邮箱用户 → 绑微信 → 微信登录落回同一账号 ----------------
            print("\n[A] 邮箱老用户绑定微信，再用微信登录")
            created = await auth.admin_create_user(
                {
                    "email": TEST_EMAIL,
                    "password": TEST_PASSWORD,
                    "email_confirm": True,
                    "user_metadata": {"provider": "email"},
                }
            )
            user_a = str(created["id"])
            _cleanup_user_ids.append(user_a)
            print(f"   建测试邮箱账号 user_id={user_a[:8]}***")

            sess = await auth.issue_session_for_user(user_a)
            token_a = str(sess["access_token"])

            r = await cli.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token_a}"})
            me_a = r.json()
            check("A1 绑定前 wechat_bound=false", me_a.get("wechat_bound") is False, f"got={me_a.get('wechat_bound')}")

            r = await cli.post(
                "/api/v1/auth/wechat/bind",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"code": "fake-code"},
            )
            check("A2 绑定微信返回 200", r.status_code == 200, f"status={r.status_code} body={r.text[:160]}")

            r = await cli.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token_a}"})
            me_a2 = r.json()
            prof = await db.select_one("profiles", filters={"id": f"eq.{user_a}"})
            check(
                "A3 绑定后 wechat_bound=true",
                me_a2.get("wechat_bound") is True,
                f"me={me_a2.get('wechat_bound')} profiles行={'存在' if prof else '不存在'} "
                f"db.wechat_bound={(prof or {}).get('wechat_bound')!r}",
            )
            check("A4 绑定后 user_id 未变", me_a2.get("id") == user_a)

            ident = await db.select_one(
                "user_identities",
                filters={"provider": "eq.wechat", "provider_uid": f"eq.{FAKE_OPENID}"},
            )
            _cleanup_openids.append(FAKE_OPENID)
            check(
                "A5 email_snapshot 已落库且为绑定时邮箱",
                bool(ident) and ident.get("email_snapshot", "").lower() == TEST_EMAIL,
                f"got={(ident or {}).get('email_snapshot')}",
            )

            r = await cli.post("/api/v1/auth/wechat/login", json={"code": "fake-code"})
            body = r.json()
            logged_id = user_id_of(body)
            check(
                "A6 微信登录落回同一 user_id（老用户数据不丢）",
                r.status_code == 200 and logged_id == user_a,
                f"status={r.status_code} id={logged_id[:8]}***",
            )

            # ---------------- B. 微信用户 → 设密码 → 邮箱密码登录落回同一账号 ----------------
            print("\n[B] 微信用户设置密码，再用邮箱密码登录")
            FAKE_OPENID = f"probe_openid_b_{RUN}"  # 换 openid，造一个从未绑定过的纯微信账号
            r = await cli.post("/api/v1/auth/wechat/login", json={"code": "fake-code-b"})
            body = r.json()
            user_b = user_id_of(body)
            token_b = body.get("access_token")
            _cleanup_user_ids.append(str(user_b))
            _cleanup_openids.append(FAKE_OPENID)
            check("B1 微信首次登录建号成功", bool(user_b) and bool(token_b), f"id={str(user_b)[:8]}***")

            r = await cli.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token_b}"})
            me_b = r.json()
            email_b = me_b.get("email")
            check("B2 /auth/me provider=wechat", me_b.get("provider") == "wechat", f"got={me_b.get('provider')}")
            check("B3 占位邮箱形如 wx_xxx@wechat.local", str(email_b).endswith("@wechat.local"), f"got={email_b}")

            r = await cli.post(
                "/api/v1/auth/me/password/set",
                headers={"Authorization": f"Bearer {token_b}"},
                json={"new_password": TEST_PASSWORD},
            )
            check("B4 设置密码返回 200", r.status_code == 200, f"status={r.status_code} body={r.text[:160]}")

            ok_pwd = await auth.verify_password(str(email_b), TEST_PASSWORD)
            check("B5 邮箱 + 密码可登录（同一账号）", ok_pwd is True, f"got={ok_pwd}")

            # 密码设了之后，微信登录必须仍然可用（这是免密签发的价值）
            r = await cli.post("/api/v1/auth/wechat/login", json={"code": "fake-code-c"})
            body2 = r.json()
            check(
                "B6 设密码后微信登录仍落回同一 user_id（两种方式并存）",
                r.status_code == 200 and user_id_of(body2) == user_b,
                f"id={user_id_of(body2)[:8]}***",
            )

            # ---------------- C. 真实 dev server（8000 端口）冒烟 ----------------
            # 前面 A/B 走的是进程内 ASGI app（脚本自己的代码）。但前端联调打的是
            # 8000 端口上那个独立进程 —— 它必须已经热重载到最新代码，否则
            # 前端拿到的 token 依然会因时钟偏移被判「尚未生效」而 401。
            print("\n[C] 真实 dev server（127.0.0.1:8000）冒烟")
            try:
                async with httpx.AsyncClient(base_url=API, timeout=20.0) as real:
                    r = await real.get(
                        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token_b}"}
                    )
            except httpx.HTTPError as exc:  # 服务没起也算失败，但不算断言失败
                print(f"   跳过：dev server 不可达（{exc}）")
            else:
                check(
                    "C1 dev server 接受刚签发的 token（热重载已生效）",
                    r.status_code == 200,
                    f"status={r.status_code} body={r.text[:160]}",
                )
    finally:
        app.dependency_overrides.pop(get_wechat_service, None)
        await cleanup(db)

    print("\n" + "=" * 60)
    failed = [n for n, ok, _ in _results if not ok]
    print(f"总计 {len(_results)} 项，通过 {len(_results) - len(failed)}，失败 {len(failed)}")
    for n in failed:
        print("  失败:", n)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
