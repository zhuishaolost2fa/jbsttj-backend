"""通过 Supabase Management API 远程执行 SQL 文件（DDL 用）。

为什么需要它：
  PostgREST **不能**执行 DDL，托管项目的 Postgres 也没有对外开放 5432
  （本地 .env 里没有 DB 直连密码），所以建表 / 建函数只能走
  Management API 的 ``POST /v1/projects/{ref}/database/query``，
  或者手工去 Dashboard -> SQL Editor 粘贴执行。本脚本就是前者的自动化版本。

用法：
  export SUPABASE_ACCESS_TOKEN=sbp_xxxx        # Dashboard -> Account -> Access Tokens
  python scripts/apply_sql_remote.py sql/user_messages.sql
  python scripts/apply_sql_remote.py sql/user_messages.sql --check   # 只打印将执行的语句

注意：
  - 凭证只从环境变量读，不落盘、不进 .env；
  - Management API 必须带 User-Agent，否则被 Cloudflare 挡（error code 1010）；
  - token 用完记得去 Dashboard Revoke。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 项目 ref 从 SUPABASE_URL 解析，避免两处硬编码不一致
REF_ENV_HINT = "SUPABASE_URL（形如 https://<ref>.supabase.co）"
TOKEN_ENV = "SUPABASE_ACCESS_TOKEN"
UA = "jbs-deploy-script/1.0"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
        load_dotenv(".env.local", override=True)
    except Exception:  # noqa: BLE001 - 没有 dotenv 就依赖真实环境变量
        pass


def _ref() -> str:
    url = os.environ.get("SUPABASE_URL", "").strip()
    if not url:
        raise SystemExit(f"缺少环境变量 SUPABASE_URL（{REF_ENV_HINT}）")
    host = url.split("//", 1)[-1].split("/", 1)[0]
    ref = host.split(".")[0]
    if not ref or ref == "supabase":
        raise SystemExit(f"无法从 SUPABASE_URL 解析项目 ref: {url}")
    return ref


def _token() -> str:
    tok = os.environ.get(TOKEN_ENV, "").strip()
    if not tok:
        raise SystemExit(
            f"请先设置环境变量 {TOKEN_ENV}\n"
            "  获取：Supabase Dashboard -> Account -> Access Tokens -> Generate new token\n"
            "  用完记得 Revoke。"
        )
    return tok


def _request(method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
    url = f"https://api.supabase.com/v1/projects/{_ref()}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    try:
        return resp.status, json.loads(raw)
    except json.JSONDecodeError:
        return resp.status, raw


def main() -> int:
    parser = argparse.ArgumentParser(description="远程执行 SQL 文件（建表 / 建函数）")
    parser.add_argument("sql_file", help="SQL 文件路径，如 sql/user_messages.sql")
    parser.add_argument(
        "--check", action="store_true", help="只打印将要执行的 SQL，不真正执行"
    )
    args = parser.parse_args()

    _load_env()
    path = Path(args.sql_file)
    if not path.is_file():
        raise SystemExit(f"找不到 SQL 文件: {path}")
    sql = path.read_text(encoding="utf-8")

    if args.check:
        print(f"[check] 项目 ref={_ref()} 文件={path} 字符数={len(sql)}")
        return 0

    status, body = _request("POST", "/database/query", {"query": sql})
    print(f"[apply] {path} -> HTTP {status}")
    print(json.dumps(body, ensure_ascii=False, indent=2)[:2000] if body else "(空响应)")
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
