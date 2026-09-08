"""给已完成解析但缺合成文章的剧本补跑 synthesis（服务器容器内执行）。

背景：finalize 里调 _run_synthesis 时引用了不存在的变量 script_title，
导致这一批的合成全部静默失败（job 照常 completed，synthesis_status 停在 pending）。
代码已修，但已在跑/已跑完的 job 不会重跑，所以这里单独补一次 —— 只跑合成，
不重跑整条流水线（不重新切块、不重新花钱生成 QA）。

用法（容器内）：
    docker cp _rerun_synthesis.py jbs-worker:/app/_rerun_synthesis.py
    docker exec jbs-worker python _rerun_synthesis.py              # 只补 pending 的
    docker exec jbs-worker python _rerun_synthesis.py --all        # 全部重生成
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.config import settings
from app.services.dm_store import get_dm_store
from app.services.supabase import get_supabase
from app.tasks.dm_ingest import _run_synthesis

DOC_SELECT = "id,script_id,script_code,total_stories,synthesis_status,is_active,updated_at"


def _get(path: str, **params):
    import httpx

    key = settings.supabase_service_role_key
    resp = httpx.get(
        f"{settings.supabase_url}/rest/v1/{path}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        params=params,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def _script_title(script_id: str) -> str:
    rows = _get("scripts", select="id,title", id=f"eq.{script_id}")
    return str(rows[0].get("title") or "") if rows else ""


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="已 ready 的也重生成")
    args = ap.parse_args()

    await get_supabase().startup()
    store = get_dm_store()

    # 本批 16 本（与 scripts/_reimport_uploaded.json 一致）
    uploaded = _get("script_dm_documents", select=DOC_SELECT, is_active="eq.true")
    docs = [d for d in uploaded if (d.get("total_stories") or 0) > 0]
    if not args.all:
        docs = [d for d in docs if (d.get("synthesis_status") or "pending") != "ready"]

    print(f"待补跑合成: {len(docs)} 本", flush=True)
    ok = fail = 0
    for d in docs:
        sid = str(d.get("script_id"))
        title = _script_title(sid)
        try:
            _run_synthesis(
                store,
                document_id=str(d.get("id")),
                script_id=sid,
                script_code=str(d.get("script_code") or ""),
                script_title=title,
            )
            ok += 1
            print(f"[ok] {title:<16} doc={d.get('id')}", flush=True)
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"[!!] {title:<16} {exc}", flush=True)
    print(f"\n完成 ok={ok} fail={fail}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
