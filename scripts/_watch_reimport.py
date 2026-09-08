"""批量重跑 DM 手册 · 后台监控：每 3 分钟查一次，全部终态或超时后退出。

用法（后台）：PYTHONPATH=. .venv/Scripts/python.exe scripts/_watch_reimport.py
日志：scripts/_reimport_watch.log
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import httpx

from app.core.config import settings
from scripts._probe_reimport_progress import JOBS  # 复用同一份清单

TERMINAL = {"completed", "failed", "cancelled", "skipped"}
INTERVAL = 180
MAX_SECONDS = 3 * 3600
LOG = "scripts/_reimport_watch.log"


def _headers() -> Dict[str, str]:
    key = settings.supabase_service_role_key
    return {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}


def _get(path: str, **params: Any) -> List[Dict[str, Any]]:
    resp = httpx.get(
        f"{settings.supabase_url}/rest/v1/{path}", headers=_headers(), params=params, timeout=60
    )
    if resp.status_code >= 400:
        print(f"!! {path} -> {resp.status_code} {resp.text[:200]}")
        return []
    return resp.json()


def _snapshot() -> Dict[str, Dict[str, Any]]:
    job_ids = ",".join(j["job_id"] for j in JOBS)
    rows = _get(
        "script_dm_jobs",
        select="id,status,stage_detail,error_message,total_chunks,embedded_chunks",
        id=f"in.({job_ids})",
    )
    return {r["id"]: r for r in rows}


def main() -> int:
    started = time.time()
    finished: Dict[str, str] = {}
    while True:
        snap = _snapshot()
        counts: Dict[str, int] = {}
        for j in JOBS:
            st = str(snap.get(j["job_id"], {}).get("status") or "?")
            counts[st] = counts.get(st, 0) + 1
            if st in TERMINAL and j["job_id"] not in finished:
                finished[j["job_id"]] = st
                row = snap[j["job_id"]]
                print(
                    f"[done] {j['title']} -> {st} "
                    f"chunks={row.get('embedded_chunks')}/{row.get('total_chunks')} "
                    f"{row.get('error_message') or ''}"
                )
        summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"[{time.strftime('%H:%M:%S')}] {summary}", flush=True)

        if len(finished) == len(JOBS):
            print("ALL DONE")
            break
        if time.time() - started > MAX_SECONDS:
            print("TIMEOUT")
            break
        time.sleep(INTERVAL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
