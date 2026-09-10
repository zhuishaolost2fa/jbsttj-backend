"""把 Supabase 的 Send Email Hook 接到自建后端（发件箱 + 反向拉取）。

为什么不是「Supabase 推给我们」：
  1. 服务器域名未备案，国际链路上的域名访问会被腾讯云重定向到
     dnspod.qcloud.com/static/webblock.html（Let's Encrypt 境外验证实测）。
  2. 更致命的是 Supabase 所在区域到服务器 80 端口 **TCP 握手就超时**
     （pg_net 实测：DNS 0.02ms、TCP handshake 12000ms）。
     HTTP Hook / Edge Function / pg_net 转发都会撞上同一堵墙。

所以改成：Postgres Hook 把事件写进 public.auth_email_outbox，我们的服务
（出网方向本来就通）定时拉取并投递。延迟 = 轮询间隔（默认 3 秒）。

用法：
  python scripts/_setup_send_email_pg_hook.py --init           # 建表 + 建函数
  python scripts/_setup_send_email_pg_hook.py --enable         # 打开 hook
  python scripts/_setup_send_email_pg_hook.py --disable
  python scripts/_setup_send_email_pg_hook.py --test           # 插一条测试事件
  python scripts/_setup_send_email_pg_hook.py --status
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request

REF = "ratcjkjvynubglofrvkt"
UPSTREAM = "http://101.34.58.31/api/v1/hooks/supabase/send-email"
HOOK_URI = "pg-functions://postgres/public/send_email_hook"

TOKEN_ENV = "SUPABASE_ACCESS_TOKEN"


def _token() -> str:
    tok = os.environ.get(TOKEN_ENV, "")
    if not tok:
        raise SystemExit(f"请先设置环境变量 {TOKEN_ENV}")
    return tok


def _req(method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
    url = f"https://api.supabase.com/v1/projects/{REF}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
            # 不带 UA 会被 Cloudflare 挡掉（error code 1010）
            "User-Agent": "jbs-deploy-script/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def sql(query: str) -> object:
    code, body = _req("POST", "/database/query", {"query": query})
    if code >= 400:
        raise SystemExit(f"SQL 失败（{code}）：{body}")
    return body


INIT_STATEMENTS = [
    """create table if not exists public.auth_email_outbox (
         id bigserial primary key,
         payload jsonb not null,
         created_at timestamptz not null default now(),
         sent_at timestamptz,
         attempts int not null default 0,
         error text
       )""",
    "alter table public.auth_email_outbox enable row level security",
    """create index if not exists idx_auth_email_outbox_pending
         on public.auth_email_outbox (id) where sent_at is null""",
    # 只写入发件箱，不做任何网络调用 —— Supabase 侧网络根本到不了我们服务器
    """create or replace function public.send_email_hook(event jsonb)
       returns jsonb language plpgsql security definer set search_path = public as $fn$
       begin
         insert into public.auth_email_outbox (payload) values (event);
         return event;   -- 返回值即最终使用的邮件数据，不能返回 null
       end;
       $fn$""",
    "grant execute on function public.send_email_hook(jsonb) to supabase_auth_admin",
    "revoke execute on function public.send_email_hook(jsonb) from anon, authenticated",
    "grant select, update on public.auth_email_outbox to service_role",
    "grant insert on public.auth_email_outbox to postgres",
]

TEST_EVENT = {
    "user": {"id": "00000000-0000-0000-0000-000000000000", "email": "relay-selftest@example.com"},
    "email_data": {
        "token": "123456",
        "token_hash": "selftest",
        "redirect_to": "https://www.jbs-ttj.store/",
        "email_action_type": "signup",
        "site_url": "https://ratcjkjvynubglofrvkt.supabase.co",
    },
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="建发件箱表 + hook 函数")
    ap.add_argument("--enable", action="store_true", help="打开 GoTrue 的 send_email hook")
    ap.add_argument("--disable", action="store_true")
    ap.add_argument("--test", action="store_true", help="手工调用一次函数")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.init:
        for stmt in INIT_STATEMENTS:
            sql(stmt)
        print("发件箱表与 hook 函数已就绪")

    if args.test:
        print(sql("select public.send_email_hook('" + json.dumps(TEST_EVENT) + "'::jsonb)"))
        print("已写入发件箱，去服务器看日志：docker compose logs --tail=40 api | grep -E 'send-email hook|已投递'")

    if args.enable:
        code, body = _req("PATCH", "/config/auth", {
            "hook_send_email_enabled": True,
            "hook_send_email_uri": HOOK_URI,
        })
        print(code, body)

    if args.disable:
        code, body = _req("PATCH", "/config/auth", {"hook_send_email_enabled": False})
        print(code, body)

    if args.status:
        code, body = _req("GET", "/config/auth")
        if isinstance(body, dict):
            for key, value in body.items():
                if "hook" in key:
                    print(f"{key} = {value}")
        else:
            print(code, body)


if __name__ == "__main__":
    main()
