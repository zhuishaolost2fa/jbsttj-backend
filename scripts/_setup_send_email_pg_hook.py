"""把 Supabase 的 Send Email Hook 接到自建后端（Postgres Hook + pg_net 转发）。

为什么是 Postgres Hook 而不是 HTTP Hook：
  服务器域名未备案，腾讯云对**国际链路**的域名访问做了拦截（Let's Encrypt 境外
  验证被重定向到 dnspod.qcloud.com/static/webblock.html），国内访问却正常。
  Supabase 在新加坡，HTTP Hook 必然走国际链路 → 不可达。Postgres Hook 从库内
  用 pg_net 直接 POST 到 **http://<IP>**（IP 不带域名，不受备案拦截）。

用法：
  python scripts/_setup_send_email_pg_hook.py --relay-token <TOKEN>
  python scripts/_setup_send_email_pg_hook.py --enable        # 打开 hook
  python scripts/_setup_send_email_pg_hook.py --test          # 手工触发一次
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


CREATE_FUNCTION = """
create or replace function public.send_email_hook(event jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions
as $fn$
declare
  v_token text := '{token}';
begin
  -- pg_net 是异步的：入队后立即返回，GoTrue 不会卡住等投递结果
  perform net.http_post(
    url := '{upstream}',
    body := event,
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'X-Relay-Token', v_token
    ),
    timeout_milliseconds := 4000
  );
  -- 返回原事件：send_email hook 的返回值即最终使用的邮件数据，不能返回 null
  return event;
end;
$fn$;

-- GoTrue 用 supabase_auth_admin 调这个函数，必须给它执行权限
grant execute on function public.send_email_hook(jsonb) to supabase_auth_admin;
revoke execute on function public.send_email_hook(jsonb) from anon, authenticated;
"""

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
    ap.add_argument("--relay-token", help="写入 SQL 函数的共享令牌")
    ap.add_argument("--enable", action="store_true", help="打开 GoTrue 的 send_email hook")
    ap.add_argument("--disable", action="store_true")
    ap.add_argument("--test", action="store_true", help="手工调用一次函数")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.relay_token:
        print(sql(CREATE_FUNCTION.format(token=args.relay_token, upstream=UPSTREAM)))
        print("函数已创建")

    if args.test:
        print(sql("select public.send_email_hook('" + json.dumps(TEST_EVENT) + "'::jsonb)"))
        print("已入队，去服务器看日志：docker compose logs --tail=40 api | grep 'send-email hook'")

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
