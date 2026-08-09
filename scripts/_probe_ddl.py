"""一次性探针：确认 Supabase 当前能否执行 DDL（建表）。

只读取，不写入。通过 PostgREST 暴露的 OpenAPI schema 枚举已存在表与 RPC 函数，
再尝试调用常见的 SQL 执行 RPC（exec_sql / run_sql / query / ...），看是否可用。
"""
import asyncio
import httpx
import os
from dotenv import load_dotenv

load_dotenv()

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
HEADERS = {
    "apikey": KEY,
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
}


async def main():
    base = f"{URL}/rest/v1"
    async with httpx.AsyncClient(timeout=20.0) as client:
        # 1) OpenAPI schema —— 枚举所有已暴露的表与 rpc 函数
        try:
            r = await client.get(
                f"{base}/",
                headers={**HEADERS, "Accept": "application/openapi+json"},
            )
            print(f"[openapi] status={r.status_code}")
            if r.status_code == 200:
                spec = r.json()
                paths = spec.get("paths", {})
                tables = sorted(
                    p[1:] for p in paths if not p.startswith("/rpc/")
                )
                rpcs = sorted(
                    p[len("/rpc/"):] for p in paths if p.startswith("/rpc/")
                )
                print("  已暴露表:", tables)
                print("  已暴露 RPC:", rpcs)
            else:
                print("  body:", r.text[:400])
        except Exception as e:  # noqa
            print("[openapi] error:", e)

        # 2) 常见 SQL 执行 RPC 探测
        rpc_names = [
            "exec_sql", "run_sql", "execute_sql", "query", "pg_exec",
            "sql", "execute", "exec", "run_query", "db_exec", "pg_query",
        ]
        for name in rpc_names:
            try:
                rr = await client.post(
                    f"{base}/rpc/{name}",
                    headers=HEADERS,
                    json={"query": "select 1 as ok"},
                )
                print(f"[rpc:{name}] status={rr.status_code} body={rr.text[:200]}")
            except Exception as e:  # noqa
                print(f"[rpc:{name}] error: {e}")

        # 3) 目标表是否已存在？
        for t in ("script_option_categories", "script_options"):
            try:
                rr = await client.get(
                    f"{base}/{t}",
                    headers=HEADERS,
                    params={"select": "code", "limit": "1"},
                )
                print(f"[table:{t}] status={rr.status_code}")
            except Exception as e:  # noqa
                print(f"[table:{t}] error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
