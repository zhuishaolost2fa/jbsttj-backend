"""临时探针：检测 Supabase 连通性、已有表、以及是否存在可执行 DDL 的 RPC。用完即删。"""
import httpx

env = {}
for line in open(".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()

URL = env["SUPABASE_URL"].rstrip("/")
SR = env["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": SR, "Authorization": f"Bearer {SR}", "Content-Type": "application/json"}
c = httpx.Client(timeout=20)

print("== 1. PostgREST 根连通性 ==")
r = c.get(f"{URL}/rest/v1/", headers=H)
print("status:", r.status_code)

print("== 2. 已有表探测 ==")
for t in ["upload_tasks", "files", "script_options", "scripts"]:
    r = c.get(f"{URL}/rest/v1/{t}", headers={**H, "Range": "0-0"}, params={"select": "*"})
    print(f"  {t:16s} -> {r.status_code} {r.text[:120]}")

print("== 3. 探测是否存在可执行 SQL 的 RPC ==")
for fn in ["exec_sql", "execute_sql", "sql", "run_sql", "query", "exec"]:
    try:
        r = c.post(f"{URL}/rest/v1/rpc/{fn}", headers=H, json={"query": "select 1"})
        print(f"  rpc/{fn:12s} -> {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"  rpc/{fn:12s} -> ERR {e}")
