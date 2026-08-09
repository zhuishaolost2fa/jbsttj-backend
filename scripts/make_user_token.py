"""为人工验证生成可用的 Supabase access_token。

流程：
1. 用 service_role key 通过 Supabase Admin API 创建一个真实验证用户（email_confirm=true）
2. 用 JWT_SECRET（HS256）为该用户签发一个结构合法的 access_token
   payload 含 sub(用户UUID) / email / role=authenticated / aud=authenticated / exp / iat
3. 若本地 uvicorn(:8000) 正在运行，自动用该 token 调 /api/v1/auth/me 验证后端放行

注意：service_role key / JWT_SECRET 均从 .env 读取，不在此打印。
仅打印生成的 access_token（1 小时有效，供粘贴到演示页）。
"""
import sys
import time
import uuid

import httpx
import jwt

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))


def main() -> int:
    from app.core.config import get_settings
    s = get_settings()

    email = "verify@jbsttj.local"
    password = "Verify#2026!Strong"

    if not s.supabase_url or not s.supabase_service_role_key:
        print("缺少 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY，无法创建用户")
        return 2
    if not s.supabase_jwt_secret:
        print("缺少 SUPABASE_JWT_SECRET，无法签发 token")
        return 2

    admin_headers = {
        "apikey": s.supabase_service_role_key,
        "Authorization": f"Bearer {s.supabase_service_role_key}",
        "Content-Type": "application/json",
    }
    base = s.supabase_url.rstrip("/")
    auth_url = f"{base}/auth/v1"

    user_id = None
    # 1) 尝试创建用户
    try:
        r = httpx.post(
            f"{auth_url}/admin/users",
            headers=admin_headers,
            json={"email": email, "password": password, "email_confirm": True},
            timeout=20,
        )
        if r.status_code < 300:
            data = r.json()
            user_id = (data.get("user") or data).get("id")
            print(f"[ok] 创建验证用户成功 id={user_id}")
        elif "already" in r.text.lower() or "exists" in r.text.lower():
            # 已存在：查询拿 id
            # 注意：Supabase Admin API 的 filter 仅接受纯文本做包含匹配，不是 OData 语法
            q = httpx.get(
                f"{auth_url}/admin/users",
                headers=admin_headers,
                params={"filter": email},
                timeout=20,
            )
            users = (q.json() or {}).get("users", [])
            if not users:
                # 兜底：拉第一页全部再客户端按 email 精确匹配
                q2 = httpx.get(
                    f"{auth_url}/admin/users",
                    headers=admin_headers,
                    params={"page": 1, "per_page": 200},
                    timeout=20,
                )
                users = (q2.json() or {}).get("users", [])
            matched = [u for u in users if (u.get("email") or "").lower() == email.lower()]
            if matched:
                user_id = matched[0].get("id")
                print(f"[ok] 用户已存在，复用 id={user_id}")
            else:
                print("用户已存在但查询不到，请检查配置")
                return 3
        else:
            print(f"创建用户失败 {r.status_code}: {r.text[:300]}")
            return 3
    except Exception as e:  # noqa: BLE001
        print(f"创建用户异常: {e}")
        return 3

    if not user_id:
        print("未能获得用户 id")
        return 3

    # 2) 签发 token
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": "authenticated",
        "aud": s.supabase_jwt_audience or "authenticated",
        "iat": now,
        "exp": now + 3600,
    }
    token = jwt.encode(payload, s.supabase_jwt_secret, algorithm="HS256")
    print("\n===== 复制下面的 access_token 到演示页「使用此 token」 =====\n")
    print(token)
    print("\n===========================================================\n")
    print("该 token 有效期 1 小时，用户:", email)

    # 3) 若本地服务在跑，自动验证
    try:
        r = httpx.get(
            "http://127.0.0.1:8000/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        print(f"[verify] GET /api/v1/auth/me -> {r.status_code}")
        if r.status_code == 200:
            print("  后端返回身份:", r.json())
        else:
            print("  响应:", r.text[:300])
    except Exception as e:  # noqa: BLE001
        print(f"[verify] 未连接到本地服务(可忽略): {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
