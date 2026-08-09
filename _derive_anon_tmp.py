"""临时诊断：用 SUPABASE_JWT_SECRET 校验 service_role key，并派生一个 role=anon 的 key，实测 Supabase 是否接受。"""
import jwt
import httpx

env = {}
with open(".env", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

sr = env["SUPABASE_SERVICE_ROLE_KEY"]
secret = env["SUPABASE_JWT_SECRET"]
base = env["SUPABASE_URL"].rstrip("/") + "/auth/v1"

# 1) 校验 service_role key 确实由该 JWT secret 签发
try:
    decoded = jwt.decode(sr, secret, algorithms=["HS256"], options={"verify_aud": False})
    print("DECODE_OK payload:", decoded)
except Exception as e:
    print("DECODE_FAIL:", repr(e))
    raise SystemExit(1)

# 2) 改写为 role=anon 并重新签名 -> 等价于官方 anon key
decoded["role"] = "anon"
anon_key = jwt.encode(decoded, secret, algorithm="HS256")
print("ANON_KEY:", anon_key)

# 3) 实测：用新 key 调 GoTrue /settings（公开端点，不创建用户）
try:
    r = httpx.get(
        base + "/settings",
        headers={"apikey": anon_key, "Authorization": f"Bearer {anon_key}"},
        timeout=15,
    )
    print("SETTINGS_STATUS:", r.status_code)
    print("SETTINGS_BODY:", r.text[:400])
except Exception as e:
    print("SETTINGS_ERR:", repr(e))
