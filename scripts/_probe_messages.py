"""站内消息（user_messages）端到端自检。

覆盖：表 / RPC 是否就位 → 幂等投递 → 列表 → 未读计数 → 标记已读 → 删除。

用法：
  python scripts/_probe_messages.py                     # 自动挑一个 profiles 里的用户
  python scripts/_probe_messages.py --user-id <uuid>     # 指定接收者

判据（别拿「没报错」当通过）：
  - 同一 dedup_key 连投两次，**第二次必须返回 None**（幂等生效）；
  - 标记已读后未读数必须 -1，重复标记返回 0（幂等且只数未读行）；
  - 删除后列表里查不到该条。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.local", override=True)

from app.services.repository import MessageRepository  # noqa: E402
from app.services.supabase import get_supabase  # noqa: E402
from app.schemas.message import MessageType  # noqa: E402


async def _pick_user() -> Optional[str]:
    db = get_supabase()
    await db.startup()
    rows = await db.select("profiles", columns="id", limit=1)
    return str(rows[0]["id"]) if rows else None


async def main(user_id: Optional[str]) -> int:
    db = get_supabase()
    await db.startup()
    repo = MessageRepository(db)

    # 1) 表 & RPC 是否就位
    try:
        await db.select("user_messages", columns="id", limit=1)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] user_messages 表不可用（{type(exc).__name__}）：{exc}")
        print("       先在 Supabase Dashboard 执行 sql/user_messages.sql，"
              "或用 scripts/apply_sql_remote.py 远程执行")
        return 1
    print("[ok] user_messages 表可读")

    if not user_id:
        user_id = await _pick_user()
    if not user_id:
        print("[SKIP] 库里没有 profiles，无法自检（用 --user-id 指定一个用户）")
        return 1
    print(f"[info] 接收者 user_id={user_id}")

    dedup = "probe:messages:selfcheck"
    data = {"scriptId": None, "scriptCode": "probe", "scriptTitle": "自检剧本"}

    # 2) 幂等投递
    first = await repo.push(
        user_id=user_id,
        msg_type=MessageType.SYSTEM,
        title="[自检] 消息系统打通",
        content="这条消息由 scripts/_probe_messages.py 写入，可安全删除",
        data=data,
        dedup_key=dedup,
    )
    print(f"[{'ok' if first else 'FAIL'}] 首次投递 -> {first}")
    if not first:
        print("       push_user_message 未返回 id（RPC 缺失或写入被拒）")
        return 1

    second = await repo.push(
        user_id=user_id,
        msg_type=MessageType.SYSTEM,
        title="[自检] 重复投递",
        content="同 dedup_key 第二次",
        data=data,
        dedup_key=dedup,
    )
    ok_idem = second is None
    print(f"[{'ok' if ok_idem else 'FAIL'}] 重复投递 -> {second}（期望 None = 幂等生效）")
    if not ok_idem:
        return 1

    # 3) 列表能查到
    rows, total = await repo.list_for_user(user_id, limit=10)
    hit = [r for r in rows if str(r.get("id")) == first]
    print(f"[{'ok' if hit else 'FAIL'}] 列表命中新消息 total={total}")
    if not hit:
        return 1

    # 4) 未读计数 → 标记已读 → 再数
    before = await repo.count_unread(user_id)
    updated = await repo.mark_read(user_id, [first])
    after = await repo.count_unread(user_id)
    ok_read = updated == 1 and after == before - 1
    print(f"[{'ok' if ok_read else 'FAIL'}] 标记已读 updated={updated} 未读 {before} -> {after}")

    again = await repo.mark_read(user_id, [first])
    print(f"[{'ok' if again == 0 else 'FAIL'}] 重复标记已读 -> {again}（期望 0）")

    # 5) settle_script_requests 可调用（无匹配时返回空数组）
    try:
        settled = await db.rpc(
            "settle_script_requests",
            {"p_script_id": None, "p_script_code": None, "p_match_keys": []},
        )
        ok_rpc = isinstance(settled, list) and not settled
        print(f"[{'ok' if ok_rpc else 'FAIL'}] settle_script_requests 空参数 -> {settled}")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] settle_script_requests 不可用：{exc}")
        return 1

    # 6) 清理
    await repo.delete(user_id, first)
    left = await repo.get(first, user_id=user_id)
    print(f"[{'ok' if left is None else 'FAIL'}] 删除后残留 -> {left}")

    return 0 if (ok_read and again == 0 and left is None) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="站内消息端到端自检")
    parser.add_argument("--user-id", default=None, help="接收者用户 ID（默认取 profiles 第一条）")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.user_id)))
