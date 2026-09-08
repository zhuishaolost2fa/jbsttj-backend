"""等待本批重跑全部到终态，然后输出最终产出汇总。

用法：PYTHONPATH=. .venv/Scripts/python.exe -u scripts/_wait_reimport_done.py [--timeout-min 240]

每 60 秒轮询一次 Supabase（jobs + documents）；全部剧本进入终态
（completed / failed / skipped / cancelled）或超时后打印汇总表并退出。
只做只读查询，不触发任何写操作，可以安全地重复运行。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx

from app.core.config import settings

PLAN_PATH = Path(__file__).resolve().parent / "_reimport_uploaded.json"
TERMINAL = {"completed", "failed", "skipped", "cancelled"}
# 页数进度在 jobs 表上（documents 表没有 extracted_pages 列），
# chunks/qa/stories 这些最终产出则要等 documents 落库后才有。
FIELDS = (
    "id,script_id,status,error_message,updated_at,total_pages,processed_pages"
)
DOC_FIELDS = "id,script_id,total_chunks,total_qa,total_stories,synthesis_status"


def _headers() -> Dict[str, str]:
    key = settings.supabase_service_role_key
    return {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}


def _get(path: str, **params: Any) -> List[Dict[str, Any]]:
    """带重试的只读查询。

    Supabase 在海外，跨洋连接偶发 SSL EOF / 超时；轮询脚本要挂几小时，
    一次抖动就整个崩掉太亏，所以失败重试 3 次仍不行才放弃（返回空当次跳过）。
    """
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = httpx.get(
                f"{settings.supabase_url}/rest/v1/{path}",
                headers=_headers(),
                params=params,
                timeout=60,
            )
            if resp.status_code >= 400:
                print(f"!! {path} -> {resp.status_code} {resp.text[:200]}", flush=True)
                return []
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - 网络抖动要重试，不能让轮询中断
            last_exc = exc
            time.sleep(3 * (attempt + 1))
    print(f"!! {path} 查询失败（已重试 3 次）: {last_exc}", flush=True)
    return []


def snapshot() -> List[Dict[str, Any]]:
    uploaded = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    titles = {x["script_id"]: x["title"] for x in uploaded}
    ids = ",".join(titles.keys())

    jobs = _get("script_dm_jobs", select=FIELDS, script_id=f"in.({ids})")
    latest: Dict[str, Dict[str, Any]] = {}
    for j in jobs:
        sid = str(j["script_id"])
        prev = latest.get(sid)
        if prev is None or str(j.get("updated_at") or "") > str(prev.get("updated_at") or ""):
            latest[sid] = j

    docs = {
        str(d["script_id"]): d
        for d in _get("script_dm_documents", select=DOC_FIELDS, script_id=f"in.({ids})")
    }

    rows = []
    for sid, title in titles.items():
        job = latest.get(sid) or {}
        doc = docs.get(sid) or {}
        rows.append(
            {
                "title": title,
                "status": job.get("status") or "-",
                "pages": f"{job.get('processed_pages') or 0}/{job.get('total_pages') or 0}",
                "chunks": doc.get("total_chunks") or 0,
                "qa": doc.get("total_qa") or 0,
                "stories": doc.get("total_stories") or 0,
                "synth": doc.get("synthesis_status") or "-",
                "error": str(job.get("error_message") or "")[:60],
            }
        )
    return rows


def _render(rows: List[Dict[str, Any]]) -> None:
    print("\n最终产出:", flush=True)
    print(f"{'剧本':<16}{'状态':<12}{'页':<10}{'块':>5}{'QA':>6}{'故事':>6}  合成", flush=True)
    print("-" * 72, flush=True)
    ok = fail = 0
    for r in rows:
        print(
            f"{r['title'][:14]:<16}{r['status']:<12}{r['pages']:<10}"
            f"{r['chunks']:>5}{r['qa']:>6}{r['stories']:>6}  {r['synth']}"
            + (f"  ERR={r['error']}" if r["error"] else ""),
            flush=True,
        )
        if r["status"] == "completed":
            ok += 1
        elif r["status"] in {"failed", "cancelled"}:
            fail += 1
    print("-" * 72, flush=True)
    print(f"完成 {ok} / 失败 {fail} / 其它 {len(rows) - ok - fail}", flush=True)


def main() -> int:
    timeout_min = 240
    if "--timeout-min" in sys.argv:
        timeout_min = int(sys.argv[sys.argv.index("--timeout-min") + 1])

    deadline = time.time() + timeout_min * 60
    last_sig = None
    while time.time() < deadline:
        rows = snapshot()
        pending = [r for r in rows if r["status"] not in TERMINAL]
        sig = [(r["title"], r["status"], r["chunks"], r["qa"], r["stories"]) for r in rows]
        if sig != last_sig:
            last_sig = sig
            tail = "" if not pending else " -> " + ", ".join(
                f"{r['title']}:{r['status']}" for r in pending[:6]
            )
            print(
                f"[{time.strftime('%H:%M:%S')}] 未完成 {len(pending)}/{len(rows)}{tail}",
                flush=True,
            )
        if not pending:
            break
        time.sleep(60)

    _render(snapshot())
    return 0


if __name__ == "__main__":
    sys.exit(main())
