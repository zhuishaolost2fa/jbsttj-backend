"""一次性探针：确认 sql/wechat_bind.sql 的两处增量变更是否已生效。

判据（PostgREST 语义）：
  - 列存在 → 200
  - 列不存在 → 42703 undefined_column
用一个已知不存在的列（total_stories_zzz）做阴性对照，避免把「网络/权限问题」误判成「列缺失」。
"""
import asyncio
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
HEADERS = {
    "apikey": KEY,
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
}

CHECKS = [
    ("user_identities", "email_snapshot"),
    ("profiles", "wechat_bound"),
    ("profiles", "provider"),
]
NEGATIVE = ("profiles", "total_stories_zzz")


async def probe(client, table: str, column: str) -> tuple[int, str]:
    """返回 (PostgREST 的 SQLSTATE, 原始响应片段)。

    注意：PostgREST 把 undefined_column 报成 HTTP 400，SQLSTATE 藏在 body 的
    code 字段里（实测如此），只看 status_code 会把 400 误判成「其他错误」。
    """
    r = await client.get(
        f"{URL}/rest/v1/{table}",
        headers=HEADERS,
        params={"select": column, "limit": "1"},
    )
    try:
        code = r.json().get("code", "")
    except Exception:  # noqa: BLE001
        code = ""
    return code or str(r.status_code), r.text[:200]


async def main():
    async with httpx.AsyncClient(timeout=20.0) as client:
        neg_status, neg_body = await probe(client, *NEGATIVE)
        print(f"[阴性对照] profiles.{NEGATIVE[1]} -> code={neg_status}")
        if neg_status != "42703":
            print("  !! 阴性对照未报 42703（应报 undefined_column），探测结果不可信:", neg_body)
        print()

        ok = True
        for table, column in CHECKS:
            status, body = await probe(client, table, column)
            if status == "200":
                verdict = "存在"
            elif status == "42703":
                verdict = "缺失"
                ok = False
            else:
                verdict = f"未知(code={status})"
                ok = False
            print(f"{table}.{column:<16} -> {verdict}")
            if status != "200":
                print("   body:", body)

        print()
        print("结论:", "全部就绪" if ok else "仍有缺失，SQL 未完全生效")


if __name__ == "__main__":
    asyncio.run(main())
